"""Verify the shipped E1 validator, schema and golden fixture before publishing."""

import json
from pathlib import Path

from .evaluation_contract import models


def run():
    contract = models()
    directory = Path(contract.__file__).parent
    golden = json.loads((directory / "evaluation-receipt.golden.json").read_text())
    contract.EvaluationSpecification.model_validate(golden["specification"])
    contract.EvaluationReceipt.model_validate(golden["receipt"])
    if json.loads((directory / "evaluation-receipt.schema.json").read_text()) != contract.EvaluationReceipt.model_json_schema():
        raise ValueError("evaluation_schema_drift")


if __name__ == "__main__":
    run()
