#!/usr/bin/env python3
"""Publish once; download by both release ID and independently recorded manifest hash."""
import argparse
import base64
import json
from pathlib import Path
import tempfile

from common import BUCKET, ACCOUNTS, REGION, identity, load, sha256, valid_id, validate_manifest


def client():
    import boto3
    return boto3.client('s3', region_name=REGION)


def put_once(s3, bucket, key, path):
    """Conditional writes are also enforced by the release bucket policy."""
    import botocore.exceptions
    path = Path(path)
    digest = sha256(path)
    if path.stat().st_size > 5 * 1024 ** 3:
        raise ValueError('Artifact exceeds the supported single-PUT size')
    try:
        with path.open('rb') as body:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*',
                          ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                          Metadata={'sha256': digest}, ServerSideEncryption='AES256')
    except botocore.exceptions.ClientError as error:
        if error.response['Error']['Code'] not in ('PreconditionFailed', '412'):
            raise
        existing = s3.head_object(Bucket=bucket, Key=key, ChecksumMode='ENABLED')
        if existing.get('ChecksumSHA256') != base64.b64encode(bytes.fromhex(digest)).decode() or existing['ContentLength'] != path.stat().st_size:
            raise ValueError('Immutable release object already exists with different content') from error


def object_key(item):
    return 'objects/sha256/' + item['sha256']


def publish(directory):
    identity(ACCOUNTS['integration-test'])
    manifest = load(directory)
    s3 = client()
    for name, item in manifest['files'].items():
        put_once(s3, BUCKET, object_key(item), directory / name)
    # A partial build/upload cannot become a visible release.
    put_once(s3, BUCKET, f"releases/{manifest['release_id']}/manifest.json", directory / 'manifest.json')
    print(sha256(directory / 'manifest.json'))


def download(directory, release_id, manifest_sha):
    import re
    valid_id(release_id)
    if not re.fullmatch('[0-9a-f]{64}', manifest_sha):
        raise ValueError('An independently recorded manifest SHA256 is required')
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Download directory must be empty')
    directory.mkdir(parents=True, exist_ok=True)
    s3 = client()
    path = directory / 'manifest.json'
    s3.download_file(BUCKET, f'releases/{release_id}/manifest.json', str(path))
    if sha256(path) != manifest_sha:
        raise ValueError('Downloaded manifest hash does not match the selected release')
    manifest = validate_manifest(json.loads(path.read_text()))
    if manifest['release_id'] != release_id:
        raise ValueError('Downloaded release ID does not match')
    for name, item in manifest['files'].items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(BUCKET, object_key(item), str(target))
    load(directory)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['publish', 'download'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--release-id')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.operation == 'publish':
        publish(args.directory)
    else:
        download(args.directory, args.release_id or '', args.manifest_sha256 or '')
