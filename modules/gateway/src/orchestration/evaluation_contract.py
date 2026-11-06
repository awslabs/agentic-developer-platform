"""Load the single shared evaluation validator from checkout or staged artifact."""

import importlib.util
import sys
from pathlib import Path

from pydantic import TypeAdapter

MULTI_REPOSITORY_SCHEMA = "multi-repository-evaluation/v1"


class EvaluationVersionError(ValueError):
    pass


def _load(name, relative):
    if name in sys.modules:
        return sys.modules[name]
    here = Path(__file__).resolve()
    paths = [here.parents[2] / relative]
    if len(here.parents) > 4:
        paths.append(here.parents[4] / relative)
    source = next((path for path in paths if path.is_file()), None)
    if source is None:
        raise ValueError("evaluation_contract_unavailable")
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise ValueError("evaluation_contract_unavailable") from None
    return module


def models():
    return _load("_adp_orchestration_evaluation_v1", Path("contracts/orchestration-evaluation/v1/models.py"))


def multi_repository_models():
    return _load("_adp_multi_repository_evaluation_v1", Path("contracts/multi-repository-evaluation/v1/models.py"))


def specification(value):
    if isinstance(value, dict) and value.get("evidence_schema") == "multi-repository-evaluation/v1":
        return multi_repository_models().EvaluationSpecification.model_validate(value)
    if isinstance(value, dict) and value.get("evidence_schema") in {"repository-evaluation/v1", "cli-live-evaluation/v1", "workflow-evaluation/v1"}:
        from .repository_evaluation_contract import native_specification

        return native_specification(value)
    if isinstance(value, dict) and value.get("evidence_schema") not in {None, "orchestration-evaluation/v1"}:
        raise EvaluationVersionError("evaluation_contract_version_unsupported")
    return models().EvaluationSpecification.model_validate(value)


def verify_multi_repository_receipt(specification_document, receipt_document, current_sources, *, now):
    """Validate v1 against trusted current sources; this does not authorize execution."""
    version = "multi-repository-evaluation/v1"
    if not isinstance(specification_document, dict) or specification_document.get("evidence_schema") != version:
        raise ValueError("evaluation_contract_version_mismatch")
    if not isinstance(receipt_document, dict) or receipt_document.get("evidence_schema") != version:
        raise ValueError("evaluation_receipt_version_mismatch")
    if not isinstance(current_sources, list) or len(current_sources) > 128:
        raise ValueError("evaluation_input_set_mismatch")
    contract = multi_repository_models()
    accepted = contract.EvaluationSpecification.model_validate(specification_document)
    receipt = contract.EvaluationReceipt.model_validate(receipt_document)
    sources = TypeAdapter(list[contract.ResolvedInput]).validate_python(current_sources)
    contract.verify_receipt(accepted, receipt, sources, now=now)
    return receipt
