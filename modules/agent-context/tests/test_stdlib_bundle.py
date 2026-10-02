"""Canonical source-bundle guards for default and alternate CPython branches."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / 'modules/gateway/security/stdlib/apply.py'
spec = importlib.util.spec_from_file_location('security_stdlib_apply', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture_bundle(tmp_path, patch_name, *, bad_after=False):
    stdlib = tmp_path / 'stdlib'
    bundle = tmp_path / 'bundle'
    stdlib.mkdir()
    bundle.mkdir()
    before = b'value = "old"\n'
    after = b'value = "fixed"\n'
    (stdlib / 'sample.py').write_bytes(before)
    manifest = {'sample.py': {'before': hashlib.sha256(before).hexdigest(),
                              'after': '0' * 64 if bad_after else hashlib.sha256(after).hexdigest()}}
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    (bundle / patch_name).write_text('--- a/Lib/sample.py\n+++ b/Lib/sample.py\n@@ -1 +1 @@\n-value = "old"\n+value = "fixed"\n')
    return stdlib, bundle, before, after


@pytest.mark.parametrize('alternate', [False, True])
def test_default_and_explicit_branch_bundle(tmp_path, alternate):
    name = 'cpython-3.11.16.patch' if alternate else 'cpython-3.13.15.patch'
    stdlib, bundle, before, after = fixture_bundle(tmp_path, name)
    options = {'patch_file': name} if alternate else {}
    module.apply_bundle(stdlib, bundle, **options)
    assert (stdlib / 'sample.py').read_bytes() == after
    with pytest.raises(RuntimeError, match='Unexpected CPython source'):
        module.apply_bundle(stdlib, bundle, **options)


def test_tampered_source_refused_before_patch(tmp_path):
    stdlib, bundle, _, _ = fixture_bundle(tmp_path, 'cpython-3.13.15.patch')
    (stdlib / 'sample.py').write_text('changed source\n')
    with pytest.raises(RuntimeError, match='Unexpected CPython source'):
        module.apply_bundle(stdlib, bundle)
    assert (stdlib / 'sample.py').read_text() == 'changed source\n'


def test_unexpected_patch_result_is_not_accepted(tmp_path):
    stdlib, bundle, _, _ = fixture_bundle(tmp_path, 'cpython-3.13.15.patch', bad_after=True)
    with pytest.raises(RuntimeError, match='Unexpected CPython source'):
        module.apply_bundle(stdlib, bundle)


@pytest.mark.parametrize('name', ['../unreviewed.patch', '/tmp/unreviewed.patch'])
def test_patch_cannot_escape_reviewed_bundle(tmp_path, name):
    with pytest.raises(ValueError, match='within the reviewed bundle'):
        module.apply_bundle(tmp_path, tmp_path, patch_file=name)


@pytest.mark.parametrize('tampered', [False, True])
def test_upstream_fixed_manifest_verifies_without_patching(tmp_path, tampered):
    stdlib, bundle, _, after = fixture_bundle(tmp_path, 'unused.patch')
    (stdlib / 'sample.py').write_bytes(after if not tampered else b'unknown source')
    manifest = {'sample.py': {'after': hashlib.sha256(after).hexdigest()}}
    (bundle / 'upstream.json').write_text(json.dumps(manifest))
    if tampered:
        with pytest.raises(RuntimeError, match='Unexpected CPython source'):
            module.apply_bundle(stdlib, bundle, verify_only=True, manifest_file='upstream.json')
    else:
        module.apply_bundle(stdlib, bundle, verify_only=True, manifest_file='upstream.json')
        assert (stdlib / 'sample.py').read_bytes() == after


@pytest.mark.parametrize('name', ['../unreviewed.json', '/tmp/unreviewed.json'])
def test_manifest_cannot_escape_reviewed_bundle(tmp_path, name):
    with pytest.raises(ValueError, match='within the reviewed bundle'):
        module.apply_bundle(tmp_path, tmp_path, verify_only=True, manifest_file=name)
