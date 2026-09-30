#!/usr/bin/env python3
"""Atomic, target-bound deployment checkpoints. Never serialize shell credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

PHASES = 'bootstrap platform gateway-infra gateway gateway-alb broker admin webhook factory context finalize frontend verify'.split()


def fingerprint(root, environment):
    # Generated backend files, factory tfvars and build outputs are deliberately
    # excluded; they are reconstructed by deployment. Hash source changes too,
    # so the same HEAD with different local code is not resumable.
    diff = subprocess.check_output(['git', '-C', str(root), 'diff', 'HEAD', '--',
                                    '.', ':!environments', ':!*.tfvars',
                                    ':!.adp-deploy-state.json'])
    digest = hashlib.sha256(diff)
    files = list((root / 'environments' / environment).rglob('*.tfvars'))
    files += [root / 'config/deployment.yml', root / 'modules/agent-context/config.local.env']
    for key in ('ADP_GATEWAY_UPDATE_TFVARS', 'ADP_PLATFORM_UPDATE_TFVARS'):
        if os.environ.get(key):
            files.append(Path(os.environ[key]).resolve())
    for path in sorted(set(files)):
        if path.is_file() and 'backend' not in path.name:
            digest.update(str(path).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def save(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def initialize(path, binding, resume=False, start=None):
    if start and start not in PHASES:
        raise ValueError('Unknown phase: ' + start + '; choose ' + ', '.join(PHASES))
    if resume:
        if not path.exists():
            raise ValueError('No deployment checkpoints exist in this checkout; run without --resume/--from')
        state = json.loads(path.read_text())
        if state.get('version') != 1 or state.get('binding') != binding:
            raise ValueError('Deployment target, source, configuration or options changed; start a new run without --resume/--from')
        index = PHASES.index(start) if start else next((i for i, phase in enumerate(PHASES) if state['phases'].get(phase) != 'complete'), len(PHASES))
        if any(state['phases'].get(phase) != 'complete' for phase in PHASES[:index]):
            raise ValueError('--from cannot skip an incomplete prerequisite')
        for phase in PHASES[index:]:
            state['phases'][phase] = 'pending'
    else:
        state = dict(version=1, binding=binding, phases=dict.fromkeys(PHASES, 'pending'))
    # Verification always observes the live target, even after a completed run.
    state['phases']['verify'] = 'pending'
    state['status'] = 'running'
    save(path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('action', choices=['init', 'status', 'running', 'complete', 'failed', 'finish'])
    parser.add_argument('phase', nargs='?')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--account')
    parser.add_argument('--region')
    parser.add_argument('--environment')
    parser.add_argument('--source')
    parser.add_argument('--options')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--from', dest='start')
    args = parser.parse_args()
    try:
        if args.action == 'init':
            binding = {key: getattr(args, key) for key in ('account', 'region', 'environment', 'source', 'options')}
            binding['inputs'] = fingerprint(args.root, args.environment)
            # Hash operator overrides, never persist their potentially secret values.
            overrides = {k: v for k, v in os.environ.items() if k.startswith(('TF_VAR_', 'TF_CLI_ARGS', 'ADP_RELEASE_'))
                         or k in ('ADP_GITHUB_ORG', 'ADP_GATEWAY_UPDATE_TFVARS', 'ADP_PLATFORM_UPDATE_TFVARS')}
            binding['overrides'] = hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest()
            initialize(args.file, binding, args.resume, args.start)
            return
        state = json.loads(args.file.read_text())
        if args.action == 'finish':
            if any(value != 'complete' for value in state['phases'].values()):
                raise ValueError('Cannot finish with incomplete phases')
            state['status'] = 'complete'
        else:
            if args.phase not in PHASES:
                raise ValueError('Unknown phase: ' + str(args.phase))
            if args.action == 'status':
                print(state['phases'][args.phase])
                return
            state['phases'][args.phase] = args.action
            if args.action == 'failed':
                state['status'] = 'failed'
        save(args.file, state)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f'Deployment checkpoint error: {error}\n')


if __name__ == '__main__':
    main()
