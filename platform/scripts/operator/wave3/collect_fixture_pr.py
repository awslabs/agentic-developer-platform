#!/usr/bin/env python3
"""Observe a disposable fixture before steering, then read its merged PR and run tests.

Uses GitHub reads and a temporary local checkout. Never dispatches, merges a PR,
changes a pod, or turns a missing observation into a successful assertion.
"""
import argparse
import base64
import hashlib
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def now():
    return datetime.now(timezone.utc).isoformat()


def gh_json(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', endpoint], timeout=60))


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(value, indent=2) + '\n')


def relative_path(value):
    if not isinstance(value, str) or not value or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('a contained relative path is required')
    return value


def file_observation(repo, path, revision, fetch=gh_json):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('file observation requires an immutable revision')
    raw = fetch(f'repos/{repo}/contents/{quote(relative_path(path), safe="/")}?ref={revision}')
    if raw.get('type') != 'file' or raw.get('path') != path or raw.get('encoding') != 'base64':
        raise ValueError('GitHub did not return the requested file')
    content = base64.b64decode(''.join(raw['content'].split()), validate=True)
    digest = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
    if raw.get('sha') != digest:
        raise ValueError('GitHub file bytes do not match their blob digest')
    return dict(repository=repo, path=path, ref=revision, sha=digest, content=content.decode('utf-8'), observed_at=now())


class _NoDTDTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("DTD declarations are not permitted in JUnit evidence")


def junit_passes(path):
    root = ET.parse(path, parser=ET.XMLParser(target=_NoDTDTreeBuilder())).getroot()
    cases = list(root.iter('testcase'))
    if not cases or any(case.find('failure') is not None or case.find('error') is not None for case in cases):
        raise ValueError('target tests are absent or failed')
    passed = sum(case.find('skipped') is None for case in cases)
    if not passed:
        raise ValueError('all target tests were skipped')
    return passed


def assemble(config, before, pr, merged_file, delivery, test_run, audit):
    if config['fixture_identity'].get('run_id') != config['fixture_run_id']:
        raise ValueError('fixture identity differs from the configured run')
    if audit.get('complete') is not True or not audit.get('observed_by') or not isinstance(audit.get('events'), list) or not audit['events']:
        raise ValueError('complete independently recorded interaction audit is required')
    events = audit['events']
    if any(not isinstance(event, dict) or event.get('phase') not in ('setup', 'task') or event.get('operation') not in ('setup', 'control', 'pod_exec', 'github_read') for event in events):
        raise ValueError('interaction audit has unclassified operations')
    if not any(event.get('operation') == 'control' and event.get('phase') == 'task' and event.get('command_id') == delivery['command_id'] for event in events):
        raise ValueError('interaction audit has no matching steer')
    manual = any(event['operation'] == 'pod_exec' and event['phase'] == 'task' for event in events)
    separate = not any(event['operation'] == 'control' and event['phase'] == 'setup' for event in events)
    if manual or not separate:
        raise ValueError('manual pod interaction or task commands during setup invalidate the pivot')
    repo, branch, target = (config[key] for key in ('authorized_fixture_repo', 'authorized_fixture_branch', 'fixture_target_path'))
    if before.get('repository') != repo or before.get('path') != target:
        raise ValueError('before snapshot belongs to another fixture')
    before_at = datetime.fromisoformat(before['observed_at'].replace('Z', '+00:00'))
    handoff_at = datetime.fromisoformat(delivery['handoff_at'].replace('Z', '+00:00'))
    if before_at > handoff_at:
        raise ValueError('before snapshot was captured after steering')
    if (pr.get('merged') is not True or (pr.get('head', {}).get('repo') or {}).get('full_name') != repo or
            pr.get('head', {}).get('ref') != branch or (pr.get('base', {}).get('repo') or {}).get('full_name') != repo or
            pr.get('base', {}).get('ref') != config['authorized_fixture_base']):
        raise ValueError('PR is unmerged or outside the authorized fixture')
    if merged_file.get('ref') != pr.get('merge_commit_sha') or merged_file.get('path') != target:
        raise ValueError('merged file is not bound to this PR')
    if before['content'] == merged_file['content'] or merged_file['content'] != config['fixture_expected_content']:
        raise ValueError('fixture did not change to the expected content')
    if test_run.get('revision') != pr['merge_commit_sha'] or test_run.get('exit_code') != 0 or test_run.get('tests_passed', 0) < 1:
        raise ValueError('target tests did not pass on the merge revision')
    return dict(fixture_identity=config['fixture_identity'], fixture_repo=repo, fixture_branch=branch,
        authorized=True, target_artifact_path=target, file_content_before=before['content'],
        file_content_after=merged_file['content'], expected_content=config['fixture_expected_content'],
        target_tests_passed=True, pr_url=pr['html_url'], pr_merged=True, merge_sha=pr['merge_commit_sha'],
        manual_pod_access=manual, operator_setup_separate_from_agent_interaction=separate, interaction_audit=audit,
        command_id=delivery['command_id'], github_pr=pr, merged_file=merged_file, target_test_runs=[test_run],
        before_snapshot=before, observed_by='collect_fixture_pr.py: GitHub API reads and exact-merge checkout tests')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['before', 'merged'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--delivery', type=Path)
    parser.add_argument('--interaction-audit', type=Path)
    parser.add_argument('--pr', type=int)
    parser.add_argument('--junit-relative')
    parser.add_argument('--test-command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    repo = config['authorized_fixture_repo']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('explicit authorized owner/repository is required')
    path = relative_path(config['fixture_target_path'])
    if args.mode == 'before':
        commit = gh_json(f'repos/{repo}/commits/{quote(config["authorized_fixture_base"], safe="")}')
        private_json(args.out, file_observation(repo, path, commit['sha']))
        return 0
    if not args.before or not args.delivery or not args.interaction_audit or not args.pr or args.pr < 1 or not args.test_command or not args.junit_relative:
        raise ValueError('merged collection requires before snapshot, delivery, PR, test command and JUnit output path')
    pr = gh_json(f'repos/{repo}/pulls/{args.pr}')
    if pr.get('merged') is not True:
        raise ValueError('PR has not been merged; collector cannot merge it')
    revision = pr['merge_commit_sha']
    merged = file_observation(repo, path, revision)
    junit = relative_path(args.junit_relative)
    with tempfile.TemporaryDirectory(prefix='adp-steered-pr-') as directory:
        checkout = Path(directory) / 'repo'
        subprocess.run(['gh', 'repo', 'clone', repo, str(checkout), '--', '--no-checkout'], check=True, timeout=120)
        subprocess.run(['git', '-C', str(checkout), 'checkout', '--detach', revision], check=True, timeout=60)
        actual = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != revision:
            raise ValueError('test checkout revision differs from the PR merge')
        # JUnit must come from this execution, not a tracked report in the fixture.
        report_path = checkout / junit
        if report_path.exists():
            raise ValueError('JUnit output already exists before target tests')
        run = subprocess.run(args.test_command, cwd=checkout, capture_output=True, timeout=600)
        private_json(args.out.with_suffix('.test-run.json'), dict(revision=revision, command=args.test_command,
            exit_code=run.returncode, stdout=run.stdout.decode(errors='replace'), stderr=run.stderr.decode(errors='replace')))
        if run.returncode != 0:
            raise ValueError('target test command failed; preserve its execution record')
        if not report_path.resolve().is_relative_to(checkout.resolve()):
            raise ValueError('JUnit report escaped the checkout')
        test_run = dict(revision=revision, command=args.test_command, exit_code=0, tests_passed=junit_passes(report_path),
                        observed_by='local exact-merge checkout; fresh JUnit report', observed_at=now())
    delivery = json.loads(args.delivery.read_text())
    delivery = delivery.get('payload', delivery)
    before = json.loads(args.before.read_text())
    before_readback = file_observation(repo, path, before['ref'])
    if any(before.get(key) != before_readback.get(key) for key in ('repository', 'path', 'ref', 'sha', 'content')):
        raise ValueError('before snapshot differs from the immutable GitHub file')
    result = assemble(config, before, pr, merged, delivery, test_run, json.loads(args.interaction_audit.read_text()))
    private_json(args.out, result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
