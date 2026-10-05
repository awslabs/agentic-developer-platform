#!/usr/bin/env python3
"""Scan issue #6999 paths and require both server-side negative controls."""

import argparse
import hashlib
from collections import Counter
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
from reconcile_6999 import ASSIGNED, EVAL, SSRF

RULES = Path(__file__).parent / 'fixtures/issue-6999-rules.yaml'
UNSAFE = Path(__file__).parent / 'fixtures/issue-6999-unsafe.txt'


def check_report(report: dict, unsafe_path: Path) -> list[dict]:
    expected = Counter((rule, path) for rule, path, _line in ASSIGNED)
    counts = Counter()
    matched = []
    for run in report.get('runs', []):
        for notification in run.get('invocations', []):
            if any(item.get('level') == 'error' for item in notification.get('toolExecutionNotifications', [])):
                raise ValueError('Semgrep emitted a scan error')
        for result in run.get('results', []):
            rule_id = result.get('ruleId', '')
            rule = next((item for item in (SSRF, EVAL) if rule_id == item or rule_id.endswith('.' + item)), None)
            physical = result['locations'][0]['physicalLocation']
            uri = physical['artifactLocation']['uri']
            line = physical['region']['startLine']
            if rule is None:
                raise ValueError('Unexpected rule in scoped candidate scan')
            if uri == str(unsafe_path) or uri.endswith('/' + unsafe_path.name):
                counts[(rule, 'unsafe')] += 1
            else:
                path = next((path for _, path, _ in ASSIGNED if uri == path or uri.endswith('/' + path)), None)
                if path is None or not any(
                    candidate_rule == rule and candidate_path == path and abs(candidate_line - line) <= 8
                    for candidate_rule, candidate_path, candidate_line in ASSIGNED
                ):
                    raise ValueError('Unassigned finding or moved scanner span: ' + uri + ':' + str(line))
                counts[(rule, path)] += 1
                matched.append({'rule': rule, 'path': path, 'line': line,
                                'accepted_suppression': any(item.get('status') == 'accepted' for item in result.get('suppressions', []))})
    expected.update({(SSRF, 'unsafe'): 1, (EVAL, 'unsafe'): 1})
    if counts != expected:
        raise ValueError('Candidate/control mismatch: ' + str(counts - expected) + ' missing ' + str(expected - counts))
    return matched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--semgrep-command', default='semgrep', help='Scanner command; pass a pinned version')
    parser.add_argument('--sarif-output', type=Path, help='Retain raw result outside this public repository')
    args = parser.parse_args()
    if args.sarif_output and args.sarif_output.resolve().is_relative_to(ROOT):
        parser.error('Raw SARIF cannot be written into this public repository')
    with tempfile.TemporaryDirectory(prefix='issue-6999-') as directory:
        fixture = Path(directory) / 'unsafe-server.js'
        fixture.write_bytes(UNSAFE.read_bytes())
        output = Path(directory) / 'candidate.sarif'
        files = list(dict.fromkeys(path for _rule, path, _line in ASSIGNED))
        command = shlex.split(args.semgrep_command) + [
            'scan', '--config', str(RULES), '--disable-nosem', '--sarif',
            '--output', str(output), '--metrics=off', *files, str(fixture),
        ]
        subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        matches = check_report(json.loads(output.read_text()), fixture)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        if args.sarif_output:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(args.sarif_output, flags, 0o600), 'wb') as retained:
                retained.write(output.read_bytes())
        print(json.dumps({'candidate_matches': len(matches), 'unsafe_controls': 2,
                          'accepted_suppressions': sum(match['accepted_suppression'] for match in matches),
                          'raw_sarif_sha256': digest}, indent=2))


if __name__ == '__main__':
    main()
