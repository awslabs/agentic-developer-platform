"""The model binding renderer must preserve data without creating YAML or shell code."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('model_root_renderer', Path(__file__).parents[1] / 'render-model-root-config.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def test_binding_survives_real_sed_and_yaml_without_interpretation():
    value = [{'workflow_ref': 'org/repo/.github/workflows/agent.yml@refs/heads/main', 'data': '" & | \\ $(ignored)\nnot a second key'}]
    replacement = renderer.render(json.dumps(value), 'bindings')
    result = subprocess.run(['sed', '-e', 's|__BINDINGS__|' + replacement + '|g'], input='BINDINGS: "__BINDINGS__"\n', capture_output=True, text=True, check=True)
    document = yaml.safe_load(result.stdout)
    assert list(document) == ['BINDINGS']
    assert json.loads(document['BINDINGS']) == value


@pytest.mark.parametrize('raw,kind', [('{}', 'bindings'), ('["not an object"]', 'bindings'), ('sha256:untrusted', 'images'), ('latest', 'images')])
def test_invalid_authority_configuration_is_rejected(raw, kind):
    with pytest.raises(ValueError):
        renderer.render(raw, kind)
