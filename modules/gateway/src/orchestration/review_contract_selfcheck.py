"""Build-time verification that an artifact really carries the review contract (#5146).

Run as ``python -m src.orchestration.review_contract_selfcheck`` inside a built image.
Exits 0 when the shared validator resolves, imports and validates the golden fixture;
exits 1 with the reasons on stderr otherwise.

Why this is a build gate rather than a runtime check
---------------------------------------------------
``contracts/orchestration-review/v1`` is a plain directory with no ``pyproject.toml``
and no package install — deliberately, so that one normative validator is shared by
the gateway, the tick and the worker instead of a copy drifting in each. Nothing in
``pip install`` puts it into an artifact. It is staged into the build context and
copied, exactly like ``pricing_policy``, and it fails the same way: the image builds
green, every test passes, and the absence surfaces in production as the first review
evidence submission of the day being refused ``contract_unavailable``.

That refusal is honest — the module fails closed by design — but it is a total outage
of the review path, discovered at the worst possible moment, and it is invisible until
someone submits evidence. So it is checked at build time, where it is a failed build.

The gateway image's docker context is ``modules/gateway``
(``codebuild/bs-gateway-build.yml`` does ``cd modules/gateway && docker build .``), so
the repository-root ``contracts/`` tree is outside the context and unreachable by
``COPY``. ``scripts/stage-contracts.sh`` copies it in first. A staging step that
silently does nothing is precisely the failure this module exists to catch, which is
why the check runs against the built artifact rather than the checkout.

What it checks, and why each part is separable
----------------------------------------------
Resolution, import and validation fail independently:

* the directory can be absent entirely (a dropped ``COPY``, an unrun staging script);
* it can be present and unimportable (a partial copy, an absent ``pydantic``);
* it can import while the golden fixture is missing, which leaves both the producer's
  and the consumer's contract suites with nothing to agree against.

So all three are asserted, and the fixture is actually *validated* rather than merely
parsed — a fixture the shipped validator rejects means the artifact's two halves
disagree, which is the #4029 drift this pattern exists to prevent.

It deliberately imports nothing from the rest of ``src/``: it must be runnable in an
artifact that carries only the contract and this module, and it must not be able to
pass because some unrelated import happened to pull the models in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Fixture keys every consumer of the golden file relies on. Named rather than
#: derived from whichever keys are present: an empty fixture would otherwise satisfy
#: a "validate everything in the file" loop vacuously.
REQUIRED_DOCUMENTS = ("accepted_result_approve", "accepted_result_approve_publication_failed")


def _candidates() -> list[Path]:
    """Where the contract may live, mirroring ``review_evidence._contract_candidates``.

    Spelled independently on purpose. Importing the gateway module would make this
    check pass or fail for reasons unrelated to what is on disk, and would drag the
    whole ``src.orchestration`` import graph into a check that must run in an artifact
    carrying almost nothing. The two are kept in step by a test that asserts they
    resolve to the same directory.
    """
    here = Path(__file__).resolve()
    relative = Path("contracts") / "orchestration-review" / "v1"
    paths = [here.parents[2] / relative]
    if len(here.parents) > 4:
        paths.append(here.parents[4] / relative)
    return paths


def run() -> list[str]:
    """Verify the contract is present, importable and agrees with its fixture.

    Returns:
        A list of failures, empty when the artifact is sound. Returning rather than
        raising so every independent problem is reported in one build log instead of
        one-per-rebuild.
    """
    failures: list[str] = []

    directory = next((path for path in _candidates() if (path / "models.py").is_file()), None)
    if directory is None:
        looked = ", ".join(str(path) for path in _candidates())
        return [
            "the review-result contract is not in this artifact (looked in: "
            f"{looked}). Review evidence would be refused 'contract_unavailable' at "
            "runtime. Stage it with scripts/stage-contracts.sh before docker build."
        ]

    import importlib.util

    spec = importlib.util.spec_from_file_location("_review_contract_selfcheck", directory / "models.py")
    if spec is None or spec.loader is None:
        return [f"{directory / 'models.py'} could not be loaded as a module"]
    models = importlib.util.module_from_spec(spec)
    # Registered before exec_module for the same reason `review_evidence` does it:
    # `models.py` defers its annotations, so pydantic resolves them via `sys.modules`
    # and an unregistered module yields a `ReviewResult` that cannot validate at all.
    # Registered under a distinct name so this check can never be satisfied by, or
    # interfere with, a module the gateway already loaded.
    sys.modules["_review_contract_selfcheck"] = models
    try:
        spec.loader.exec_module(models)
    except Exception as exc:  # pragma: no cover - exercised by the build, not tests
        sys.modules.pop("_review_contract_selfcheck", None)
        return [f"cannot import the contract validator at {directory / 'models.py'}: {exc!r}"]

    for name in ("ReviewResult", "PublicationOutcome", "ReviewVerdict", "CONTRACT_NAME"):
        if not hasattr(models, name):
            failures.append(f"the contract validator does not export {name!r}")

    golden_path = directory / "review-result.golden.json"
    if not golden_path.is_file():
        failures.append(f"the golden fixture is missing at {golden_path}; producer and consumer would have no shared artifact to agree against")
        return failures

    try:
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [*failures, f"the golden fixture at {golden_path} could not be read: {exc!r}"]

    if not hasattr(models, "ReviewResult"):
        return failures

    for key in REQUIRED_DOCUMENTS:
        document = golden.get(key)
        if not isinstance(document, dict):
            failures.append(f"the golden fixture has no {key!r} document")
            continue
        payload = {name: value for name, value in document.items() if not name.startswith("$")}
        try:
            models.ReviewResult.model_validate(payload)
        except Exception as exc:
            failures.append(
                f"the shipped validator rejects the shipped fixture {key!r}: {exc!r}. The artifact's producer and consumer halves disagree."
            )

    return failures


def main() -> int:
    failures = run()
    if failures:
        print("Review-result contract selfcheck FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Review-result contract selfcheck OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
