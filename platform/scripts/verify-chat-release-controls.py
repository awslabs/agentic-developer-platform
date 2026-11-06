#!/usr/bin/env python3
"""Verify operator-owned Chat admission/RBAC prerequisites without mutating them."""
import copy
import json
import subprocess
import sys


def canonical(document):
    value = copy.deepcopy(document)
    metadata = value.get('metadata', {})
    value['metadata'] = {k: metadata[k] for k in ('name', 'namespace') if k in metadata}
    value.pop('status', None)
    # API-server defaults that do not change the declared admission match.
    for key in ('matchConstraints', 'matchResources'):
        match = value.get('spec', {}).get(key, {})
        for field, default in (('matchPolicy', 'Equivalent'), ('namespaceSelector', {}), ('objectSelector', {})):
            if match.get(field) == default:
                match.pop(field, None)
    for subject in value.get('subjects', []):
        if subject.get('kind') == 'ServiceAccount' and subject.get('apiGroup') == '':
            subject.pop('apiGroup')
    return value


def objects(document):
    return document['items'] if document.get('kind') == 'List' else [document]


def identity(document):
    return document['kind'], document['metadata'].get('namespace', ''), document['metadata']['name']


def verify(expected, actual):
    desired = {identity(item): canonical(item) for item in objects(expected)}
    live = {identity(item): canonical(item) for item in objects(actual)}
    if desired != live:
        changed = sorted(key for key in desired.keys() | live.keys() if desired.get(key) != live.get(key))
        raise RuntimeError(f'Chat release prerequisites differ: {changed}. Apply the reviewed controls with the platform operator before retrying; the release identity cannot change them.')


def decode_documents(output):
    documents = []
    decoder = json.JSONDecoder()
    while output.strip():
        document, end = decoder.raw_decode(output.lstrip())
        documents.extend(objects(document))
        output = output.lstrip()[end:]
    if not documents:
        raise RuntimeError('kubectl returned no Chat control objects')
    return {'kind': 'List', 'items': documents}


def kubectl(*args):
    return decode_documents(subprocess.check_output(['kubectl', *args], text=True))


def main(paths):
    for path in paths:
        expected = kubectl('create', '--dry-run=client', '--validate=false', '-f', path, '-o', 'json')
        actual = kubectl('get', '-f', path, '-o', 'json')
        verify(expected, actual)
    print('Existing Chat admission and RBAC prerequisites match the reviewed manifests.')


if __name__ == '__main__':
    main(sys.argv[1:])
