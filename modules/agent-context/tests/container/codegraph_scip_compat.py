"""Offline acceptance of patched CGC protobuf bindings and actual SCIP parser.

Mount the retained fixture directory at /fixtures and writable scratch at /tmp.
The fixture was emitted by the unpatched CGC0.6.13/protobuf3.20.3 runtime.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import tempfile

from codegraphcontext.tools import scip_pb2
from codegraphcontext.tools.scip_indexer import ScipIndexParser
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions; do not use Python -O')
    version = importlib.metadata.version('protobuf')
    assert int(version.split('.')[0]) >= 5, 'legacy runtime does not establish migration acceptance'
    fixture = Path('/fixtures')
    wire = (fixture / 'legacy-3.20.3.scip').read_bytes()
    index = scip_pb2.Index.FromString(wire)
    assert MessageToDict(index, preserving_proto_field_name=True) == json.loads(
        (fixture / 'legacy-3.20.3.json').read_text())
    # Includes an unknown field: forward-compatible data must survive reencoding.
    assert index.SerializeToString() == wire
    assert ScipIndexParser().parse(fixture / 'legacy-3.20.3.scip', fixture / 'repo') == json.loads(
        (fixture / 'legacy-parser-output.json').read_text())
    assert scip_pb2.SymbolRole.Definition == 1
    assert scip_pb2.SymbolInformation.Function == 17
    try:
        scip_pb2.Index.FromString(wire[:-1])
    except DecodeError:
        pass
    else:
        raise AssertionError('truncated wire accepted')
    with tempfile.TemporaryDirectory() as temporary:
        malformed = Path(temporary) / 'malformed.scip'
        malformed.write_bytes(wire[:-1])
        assert ScipIndexParser().parse(malformed, fixture / 'repo') == {}
        reencoded = Path(temporary) / 'roundtrip.scip'
        reencoded.write_bytes(index.SerializeToString())
        assert ScipIndexParser().parse(reencoded, fixture / 'repo')['symbol_table']
    distribution = importlib.metadata.distribution('codegraphcontext')
    provenance = json.loads(distribution.read_text('adp-protobuf-migration.json'))
    assert hashlib.sha256(scip_pb2.DESCRIPTOR.serialized_pb).hexdigest() == provenance['serialized_descriptor_sha256']
    assert distribution.version == '0.6.13'
    print(json.dumps({'protobuf': version, 'codegraphcontext': distribution.version,
                      'legacy_wire': 'roundtrip-identical', 'actual_scip_parser': 'passed',
                      'malformed_wire': 'rejected', 'patch_provenance': provenance}))


if __name__ == '__main__':
    main()
