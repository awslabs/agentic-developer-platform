"""Boundary tests for the local CGC wheel repair; no downloads or pip mutation."""
import ast
import base64
import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import zipfile

import pytest
from google.protobuf.json_format import MessageToDict

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/scip-compat'
spec = importlib.util.spec_from_file_location('patch_cgc', ROOT / 'images/shared/patch-codegraph-wheel.py')
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


def wheel(tmp_path, monkeypatch, requirement='Requires-Dist: protobuf<3.21,>=3.20'):
    descriptor = (FIXTURES / 'schema.pb').read_bytes()
    path = tmp_path / 'upstream.whl'
    files = {
        patcher.MODULE: f'DESCRIPTOR = _descriptor.FileDescriptor(serialized_pb={descriptor!r})'.encode(),
        f'{patcher.DIST}/METADATA': requirement.encode(),
        f'{patcher.DIST}/RECORD': b'',
        'codegraphcontext/unchanged.py': b'ORIGINAL_PAYLOAD = 42\n',
    }
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    monkeypatch.setattr(patcher, 'UPSTREAM_SHA256', hashlib.sha256(path.read_bytes()).hexdigest())
    return path, files


def test_unrecognized_wheel_is_refused_before_any_output(tmp_path):
    source = tmp_path / 'untrusted.whl'
    source.write_bytes(b'untrusted artifact')
    destination = tmp_path / 'output.whl'
    with pytest.raises(ValueError, match='wheel hash'):
        patcher.patch_wheel(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize('requirement', ['Requires-Dist: protobuf<4',
                                       'Requires-Dist: protobuf<3.21,>=3.20\n' * 2])
def test_unexpected_metadata_is_refused(tmp_path, monkeypatch, requirement):
    source, _ = wheel(tmp_path, monkeypatch, requirement)
    with pytest.raises(ValueError, match='protobuf requirement'):
        patcher.patch_wheel(source, tmp_path / 'output.whl')


def test_patched_wire_schema_and_record_are_preserved(tmp_path, monkeypatch):
    source, original = wheel(tmp_path, monkeypatch)
    destination = tmp_path / 'patched.whl'
    patcher.patch_wheel(source, destination)
    with zipfile.ZipFile(destination) as archive:
        assert archive.read('codegraphcontext/unchanged.py') == original['codegraphcontext/unchanged.py']
        assert b'Requires-Dist: protobuf>=5.29.6,<8' in archive.read(f'{patcher.DIST}/METADATA')
        rows = list(csv.reader(io.StringIO(archive.read(f'{patcher.DIST}/RECORD').decode())))
        assert {row[0] for row in rows} == set(archive.namelist())
        for name, expected, size in rows:
            if name.endswith('/RECORD'):
                assert expected == size == ''
                continue
            content = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
            assert expected == 'sha256=' + actual
            assert int(size) == len(content)
        module = archive.read(patcher.MODULE)
    ast.parse(module)
    namespace = {'__name__': 'fixture_scip_pb2'}
    exec(compile(module, 'fixture_scip_pb2.py', 'exec'), namespace)
    wire = (FIXTURES / 'legacy-3.20.3.scip').read_bytes()
    decoded = namespace['Index'].FromString(wire)
    assert decoded.SerializeToString() == wire
    assert decoded.documents[0].symbols[0].display_name == 'greet'
    assert MessageToDict(decoded, preserving_proto_field_name=True)['documents'][0]['relative_path'] == 'sample.py'
