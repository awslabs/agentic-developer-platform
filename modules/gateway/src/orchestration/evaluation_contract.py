"""Load the single shared evaluation validator from checkout or staged artifact."""

import importlib.util
import sys
from pathlib import Path


def models():
    name = "_adp_orchestration_evaluation_v1"
    if name in sys.modules:
        return sys.modules[name]
    here = Path(__file__).resolve()
    relative = Path("contracts/orchestration-evaluation/v1/models.py")
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


def specification(value):
    return models().EvaluationSpecification.model_validate(value)
