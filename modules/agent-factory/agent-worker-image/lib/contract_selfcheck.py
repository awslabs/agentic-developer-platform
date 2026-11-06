"""Build-time verification that this image really carries the shared contracts (#5146).

Run as ``python3 -m lib.contract_selfcheck`` inside a built agent-worker image. Exits
0 when the shared review-result validator resolves, imports and agrees with its golden
fixture and with this image's producer; exits 1 with the reasons on stderr.

Why this is a build gate
------------------------
``contracts/orchestration-review/v1`` holds ONE normative validator, read by the
gateway, the orchestration tick and this worker, so the producer here and the consumer
there cannot drift into disagreeing — the #4029 failure, where two suites each asserted
their own assumption and both stayed green for months while the two sides disagreed in
production.

It is not pip-installed (deliberately: installing it would put a copy per consumer back
in place), so nothing in the dependency resolution puts it into this image. It arrives
by a single ``COPY contracts/ /app/contracts/`` line. A dropped line, a changed build
context, or a ``.dockerignore`` entry all produce an image that builds green, passes
every test, and silently degrades: ``review_result.contract_models()`` returns ``None``
and the producer emits its artifact unvalidated again.

That degradation is by design at *runtime* — a reviewer run must not lose a delivered
review's evidence because a build dropped a directory. But it must not be how the image
normally operates, and nothing about the running pod would reveal it. So it is checked
here, where its absence is a failed build.

What it checks, and why each is separable
-----------------------------------------
* the contract resolves and imports (absence, partial copy, absent pydantic);
* the golden fixture is present and the shipped validator **accepts** it — a fixture
  the shipped validator rejects means the image's two halves already disagree;
* this image's own producer agrees with the shipped validator about the contract's
  identity. ``review_result`` spells ``CONTRACT_NAME``/``CONTRACT_VERSION`` locally
  because a document must carry them before any validator is consulted, and a local
  copy that has drifted from the contract emits documents the gateway refuses as
  ``wrong_contract`` — which is exactly the drift this whole pattern exists to stop,
  so the image refuses to ship carrying both.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

from pydantic import TypeAdapter


def run() -> list[str]:
    """Verify the contract is present, importable, and agrees with this image.

    Returns:
        A list of failures, empty when the image is sound. Returned rather than raised
        so every independent problem appears in one build log.
    """
    failures: list[str] = []

    # Imported here rather than at module scope so a broken `review_result` is reported
    # as a failure by this check instead of crashing the check itself.
    try:
        from lib.review_result import (
            CONTRACT_NAME,
            CONTRACT_VERSION,
            _contract_candidates,
            contract_models,
        )
    except Exception as exc:  # noqa: BLE001 - a build gate reports, it does not crash
        return [f"cannot import lib.review_result: {exc!r}"]

    models = contract_models()
    if models is None:
        looked = ", ".join(_contract_candidates())
        return [
            (
                "this image does not carry the shared review-result contract (looked in: "
                f"{looked}). The producer would emit unvalidated artifacts. Check the "
                "`COPY contracts/` line in the Dockerfile and the build context."
            )
        ]

    for name in ("ReviewResult", "CONTRACT_NAME", "CONTRACT_VERSION"):
        if not hasattr(models, name):
            failures.append(f"the contract validator does not export {name!r}")

    # The producer's local constants versus the contract's own. A disagreement here
    # means every document this image emits is refused `wrong_contract` by the gateway.
    if getattr(models, "CONTRACT_NAME", None) != CONTRACT_NAME:
        failures.append(
            f"producer CONTRACT_NAME {CONTRACT_NAME!r} != contract "
            f"{getattr(models, 'CONTRACT_NAME', None)!r}"
        )
    if getattr(models, "CONTRACT_VERSION", None) != CONTRACT_VERSION:
        failures.append(
            f"producer CONTRACT_VERSION {CONTRACT_VERSION!r} != contract "
            f"{getattr(models, 'CONTRACT_VERSION', None)!r}"
        )

    directory = next(
        (
            path
            for path in _contract_candidates()
            if os.path.isfile(os.path.join(path, "models.py"))
        ),
        None,
    )
    if directory is None:  # pragma: no cover - contract_models() already resolved one
        return [*failures, "the contract resolved but its directory could not be located"]

    golden_path = os.path.join(directory, "review-result.golden.json")
    if not os.path.isfile(golden_path):
        failures.append(
            f"the golden fixture is missing at {golden_path}; producer and consumer would "
            "have no shared artifact to agree against"
        )
        return failures

    try:
        with open(golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [*failures, f"the golden fixture at {golden_path} could not be read: {exc!r}"]

    if not hasattr(models, "ReviewResult"):
        return failures

    # Named keys rather than "everything in the file": an empty or renamed fixture must
    # fail rather than satisfy the loop vacuously.
    for key in ("accepted_result_approve", "accepted_result_approve_publication_failed"):
        document = golden.get(key)
        if not isinstance(document, dict):
            failures.append(f"the golden fixture has no {key!r} document")
            continue
        payload = {name: value for name, value in document.items() if not name.startswith("$")}
        try:
            models.ReviewResult.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - any rejection, however raised, is a failure
            failures.append(
                f"the shipped validator rejects the shipped fixture {key!r}: {exc!r}. "
                "This image's producer and validator disagree."
            )

    multi_directory = os.path.join(
        os.path.dirname(os.path.dirname(directory)), "multi-repository-evaluation", "v1"
    )
    multi_source = os.path.join(multi_directory, "models.py")
    multi_golden_path = os.path.join(multi_directory, "evaluation-receipt.golden.json")
    if not os.path.isfile(multi_source) or not os.path.isfile(multi_golden_path):
        failures.append(
            "this image does not carry the complete multi-repository-evaluation/v1 contract"
        )
        return failures
    try:
        module_name = "_adp_worker_multi_repository_evaluation_v1"
        source_spec = importlib.util.spec_from_file_location(module_name, multi_source)
        multi_models = importlib.util.module_from_spec(source_spec)
        sys.modules[module_name] = multi_models
        source_spec.loader.exec_module(multi_models)
        with open(multi_golden_path, encoding="utf-8") as handle:
            multi_golden = json.load(handle)
        multi_specification = multi_models.EvaluationSpecification.model_validate(
            multi_golden["specification"]
        )
        multi_receipt = multi_models.EvaluationReceipt.model_validate(multi_golden["receipt"])
        if (
            multi_models.specification_digest(multi_specification)
            != multi_golden["expected_specification_digest"]
        ):
            failures.append("multi-repository-evaluation specification digest drift")
        if (
            multi_models.input_set_digest(multi_specification, multi_receipt.sources)
            != multi_golden["expected_input_set_digest"]
        ):
            failures.append("multi-repository-evaluation input set digest drift")
        current_sources = TypeAdapter(list[multi_models.ResolvedInput]).validate_python(
            multi_golden["sources"]
        )
        multi_models.verify_receipt(
            multi_specification, multi_receipt, current_sources, now=multi_receipt.observed_at
        )
    except Exception as exc:
        failures.append(
            f"the shipped multi-repository-evaluation/v1 contract rejects its fixture: {exc!r}"
        )
    finally:
        sys.modules.pop("_adp_worker_multi_repository_evaluation_v1", None)

    return failures


def main() -> int:
    failures = run()
    if failures:
        print("Shared-contract selfcheck FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Shared-contract selfcheck OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
