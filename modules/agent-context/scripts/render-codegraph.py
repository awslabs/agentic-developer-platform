#!/usr/bin/env python3
"""Render a CodeGraph manifest only for an image matching a passed runtime receipt.

This command is offline and never invokes Kubernetes or a registry. Receipt files
are trusted operator review artifacts, not a cryptographic signing mechanism.
"""

import argparse
import json
import re
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]


def image_digest(reference):
    if not isinstance(reference, str) or reference.count('@') != 1:
        raise ValueError('Image must be a complete repository@sha256 reference')
    repository, digest = reference.split('@')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or len(repository) > 255:
        raise ValueError('Image must use an immutable sha256 digest')
    segments = repository.split('/')
    if len(segments) > 1 and ('.' in segments[0] or ':' in segments[0] or segments[0] == 'localhost'):
        registry = segments.pop(0)
        host, separator, port = registry.partition(':')
        label = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
        if not re.fullmatch(label + r'(?:\.' + label + r')*', host):
            raise ValueError('Invalid image registry host')
        if separator and (not re.fullmatch(r'[0-9]+', port) or not 1 <= int(port) <= 65535):
            raise ValueError('Invalid image registry port')
    component = r'[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*'
    if not segments or any(not re.fullmatch(component, part) for part in segments):
        raise ValueError('Invalid image repository path')
    return digest


def render(image, receipt_path, namespace, service_account, variant='current'):
    digest = image_digest(image)
    receipt = json.loads(Path(receipt_path).read_text())
    if (not isinstance(receipt, dict) or receipt.get('result') != 'passed'
            or receipt.get('fixture') != 'codegraph-runtime-v1'):
        raise ValueError('CodeGraph runtime receipt must record a passed result')
    for key, length in [('revision', 40), ('source_archive_sha256', 64)]:
        if not re.fullmatch(f'[0-9a-f]{{{length}}}', str(receipt.get(key, ''))):
            raise ValueError(f'CodeGraph runtime receipt has invalid {key}')
    repo_digests = receipt.get('image_repo_digests', [])
    if not isinstance(repo_digests, list):
        raise TypeError('CodeGraph runtime receipt has invalid repository digests')
    identities = [image_digest(value) for value in repo_digests]
    if digest not in identities:
        raise ValueError('Requested image digest has no matching passed CodeGraph runtime receipt')
    for name, value in [('namespace', namespace), ('service account', service_account)]:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
            raise ValueError(f'Invalid Kubernetes {name}')
    path = 'manifests/codegraph.yaml' if variant == 'current' else 'kubernetes/codegraph-deployment.yaml'
    return Template((ROOT / path).read_text()).substitute(
        CODEGRAPH_IMAGE=image, NAMESPACE=namespace, SERVICE_ACCOUNT=service_account,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--namespace', default='agent-context')
    parser.add_argument('--service-account', default='agent-context-sa')
    parser.add_argument('--variant', choices=['current', 'legacy'], default='current')
    args = parser.parse_args()
    try:
        output = render(args.image, args.receipt, args.namespace, args.service_account, args.variant)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'CodeGraph render refused: {exc}\n')
    print(output, end='')


if __name__ == '__main__':
    main()
