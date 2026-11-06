"""Verify the shipped E1 validator, schema and golden fixture before publishing."""

import json
from pathlib import Path

from .evaluation_contract import models, multi_repository_models, specification, verify_multi_repository_receipt


def run():
    contract = models()
    directory = Path(contract.__file__).parent
    golden = json.loads((directory / "evaluation-receipt.golden.json").read_text())
    legacy = contract.EvaluationSpecification.model_validate(golden["specification"])
    if specification(golden["specification"]).model_dump(mode="json") != legacy.model_dump(mode="json"):
        raise ValueError("evaluation_legacy_selector_drift")
    contract.EvaluationReceipt.model_validate(golden["receipt"])
    if json.loads((directory / "evaluation-receipt.schema.json").read_text()) != contract.EvaluationReceipt.model_json_schema():
        raise ValueError("evaluation_schema_drift")

    multi_contract = multi_repository_models()
    multi_directory = Path(multi_contract.__file__).parent
    multi_golden = json.loads((multi_directory / "evaluation-receipt.golden.json").read_text())
    multi_specification = specification(multi_golden["specification"])
    multi_receipt = multi_contract.EvaluationReceipt.model_validate(multi_golden["receipt"])
    if multi_contract.specification_digest(multi_specification) != multi_golden["expected_specification_digest"]:
        raise ValueError("multi_repository_specification_drift")
    if multi_contract.input_set_digest(multi_specification, multi_receipt.sources) != multi_golden["expected_input_set_digest"]:
        raise ValueError("multi_repository_input_set_drift")
    verify_multi_repository_receipt(multi_golden["specification"], multi_golden["receipt"], multi_golden["sources"], now=multi_receipt.observed_at)


if __name__ == "__main__":
    run()
