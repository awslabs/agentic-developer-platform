#!/usr/bin/env python3
"""Collect bounded rejecting gateway requests; assemble later runtime observations.

Never sends a valid owner command to a live run. Runtime token/transport probes
and side-effect counters must be collected independently for these command IDs.
"""
import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from collect_fixture_pr import private_json

VERBS = ('pause', 'resume', 'steer', 'abort')
PATHS = {'activity': '/activity/invocations/{run_id}/agent/{verb}',
         'orchestration': '/orchestration/runs/{run_id}/{verb}'}


def runtime_counters(config, observation):
    """Bind private progress and an actual listener journal read to this worker."""
    progress, state = observation['progress'], observation['state']
    if (progress.get('invocation_id') != config['live_run_id'] or
        progress.get('run_id') != config['runtime_run_id'] or
        progress.get('source_revision') != config['runtime_revision'] or
        progress.get('generation') != config['runtime_generation'] or
        state.get('generation') != config['runtime_generation'] or
        observation.get('pod_uid') != config['runtime_pod_uid']):
        raise ValueError('runtime observation identity differs from the configured worker')
    if progress.get('counters_complete') is not True or progress.get('dropped_events') != 0:
        raise ValueError('runtime counters are incomplete')
    if progress.get('active_tools') != 0:
        raise ValueError('runtime must be quiescent during rejection probes')
    if not observation.get('observed_by') or not observation.get('observed_at'):
        raise ValueError('runtime observation provenance is missing')
    datetime.fromisoformat(observation['observed_at'].replace('Z', '+00:00'))
    commands = state.get('commands')
    if not isinstance(commands, list):
        raise ValueError('actual listener journal is missing')
    ids = [row.get('command_id') for row in commands]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise ValueError('listener journal command identities are invalid')
    counts = dict(accepted_commands=len(ids), sdk_queries=progress.get('sdk_queries'),
                  tool_starts=progress.get('tool_starts'))
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError('runtime counters must be observed nonnegative integers')
    return counts, set(ids)


def collect_gateway(config, request, observe_runtime=None):
    base = config['gateway_url'].rstrip('/')
    if urlsplit(base).scheme != 'https':
        raise ValueError('fixture gateway must use HTTPS')
    identity = config['fixture_identity']
    if identity['run_id'] != config['fixture_run_id']:
        raise ValueError('fixture run identity differs')
    tokens = {role: os.environ[config['identity_env'][role]] for role in ('owner', 'nonowner', 'other_tenant')}
    if not all(tokens.values()):
        raise ValueError('all three fixture identities must be authenticated')
    result = dict(fixture_identity=identity, verbs=list(VERBS), auth_matrix=[], malformed_payloads=[],
                  terminal_generation=[], observed_by='collect_security.py: actual HTTPS gateway responses')
    if observe_runtime:
        result.update(side_effects={}, runtime_observations={}, observation_errors=[])
    for adapter, template in PATHS.items():
        for verb in VERBS:
            cases = [('auth_matrix', case, role, config['unknown_run_id'] if case == 'unknown' else config['live_run_id'])
                     for case, role in (('anonymous', None), ('nonowner', 'nonowner'), ('other_tenant', 'other_tenant'), ('unknown', 'owner'))]
            cases += [('malformed_payloads', case, 'owner', config['live_run_id']) for case in ('json', 'actor', 'target', 'token', 'oversized')]
            cases += [('terminal_generation', role, role, config['terminal_run_id']) for role in tokens]
            for group, case, role, invocation in cases:
                command_id = str(uuid.uuid4())
                body = {'command_id': command_id}
                if verb == 'steer': body['instruction'] = 'Disposable fixture refusal probe'
                if group == 'malformed_payloads' and case in ('actor', 'target', 'token'):
                    body[case] = 'must-be-rejected'
                content = b'{not-json' if case == 'json' else b'x' * (32 * 1024) if case == 'oversized' else json.dumps(body).encode()
                headers = {'Content-Type': 'application/json'}
                if role is not None: headers['Authorization'] = 'Bearer ' + tokens[role]
                url = base + template.format(run_id=quote(invocation, safe=''), verb=verb)
                started = datetime.now(timezone.utc).isoformat()
                before = None
                if observe_runtime:
                    try:
                        before = observe_runtime()
                        runtime_counters(config, before)
                    except (ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                        result['observation_errors'].append(command_id)
                        return result  # Refuse before sending; retain earlier responses.
                status, response_body = request(url, headers, content)
                result[group].append(dict(adapter=adapter, verb=verb, case=case, command_id=command_id,
                    run_id=invocation, status=status, response_body=response_body, started_at=started,
                    observed_at=datetime.now(timezone.utc).isoformat(), observed_by='POST ' + url))
                if observe_runtime:
                    try:
                        after = observe_runtime()
                        before_counts, before_ids = runtime_counters(config, before)
                        after_counts, after_ids = runtime_counters(config, after)
                        if not before_ids <= after_ids:
                            raise ValueError('journal entries disappeared during observation')
                        result['runtime_observations'][command_id] = dict(before=before, after=after)
                        result['side_effects'][command_id] = dict(before=before_counts, after=after_counts,
                            observed_by=before['observed_by'] + ' / ' + after['observed_by'])
                    except (ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                        # Preserve the actual response even when its measurement fails.
                        result['observation_errors'].append(command_id)
                        return result
    return result


def assemble(gateway, runtime):
    if gateway.get('fixture_identity') != runtime.get('fixture_identity'):
        raise ValueError('runtime observations belong to another fixture')
    required = ('token_expiry', 'stale_generation', 'unsafe_targets', 'side_effects')
    if any(key not in runtime for key in required):
        raise ValueError('runtime security observations are incomplete')
    result = dict(gateway)
    result.update({key: runtime[key] for key in required})
    if gateway.get('observation_errors'):
        raise ValueError('gateway rejection runtime observations failed')
    measured = gateway.get('side_effects', {})
    if measured:
        if set(measured) & set(runtime['side_effects']):
            raise ValueError('runtime assembly cannot overwrite gateway measurements')
        result['side_effects'] = {**measured, **runtime['side_effects']}
    expected = {row['command_id'] for group in ('auth_matrix', 'malformed_payloads', 'terminal_generation', 'token_expiry', 'stale_generation') for row in result[group]}
    if not isinstance(result['side_effects'], dict) or set(result['side_effects']) != expected:
        raise ValueError('runtime counters do not cover these exact rejected commands')
    result['runtime_observed_by'] = runtime.get('observed_by')
    if not result['runtime_observed_by']:
        raise ValueError('runtime capture provenance is required')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['capture', 'assemble'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--gateway-capture', type=Path)
    parser.add_argument('--runtime-capture', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'capture':
        if not args.config:
            raise ValueError('capture requires explicit fixture configuration')
        import httpx
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            def request(url, headers, content):
                response = client.post(url, headers=headers, content=content)
                try: body = response.json()
                except ValueError: body = response.text
                return response.status_code, body
            config = json.loads(args.config.read_text())
            observer_command = config.get('runtime_observer_command')
            observer = None
            if observer_command is not None:
                if (not isinstance(observer_command, list) or not observer_command or
                    any(not isinstance(arg, str) or not arg for arg in observer_command)):
                    raise ValueError('runtime_observer_command must be an explicit argv list')
                def observer():
                    raw = subprocess.run(observer_command, capture_output=True, text=True,
                                         timeout=30, check=True)
                    return json.loads(raw.stdout)
            result = collect_gateway(config, request, observer)
        private_json(args.out, result)
        # A capture is intentionally partial until the independent runtime probes
        # and counters arrive. It is not an acceptance result.
        return 0
    if not args.gateway_capture or not args.runtime_capture:
        raise ValueError('assembly requires both independently captured documents')
    result = assemble(json.loads(args.gateway_capture.read_text()), json.loads(args.runtime_capture.read_text()))
    private_json(args.out, result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
