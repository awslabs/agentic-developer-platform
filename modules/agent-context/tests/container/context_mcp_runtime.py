"""Start the shipped MCP server offline; verify auth and ACL-outage boundaries."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def request(path, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:5100' + path,
                                                          headers=headers or {}), timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    log = open('/tmp/context-mcp.log', 'w')
    process = subprocess.Popen(['uvicorn', 'door.server:app', '--host', '127.0.0.1', '--port', '5100'],
                               stdout=log, stderr=log)
    try:
        ready = False
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError(Path('/tmp/context-mcp.log').read_text())
            try:
                code, body = request('/health')
                ready = code == 200 and body == {'status': 'ok'}
                if ready:
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.5)
        assert ready, Path('/tmp/context-mcp.log').read_text()
        assert request('/tools')[0] == 401
        assert request('/tools', {'X-Internal-Api-Key': 'wrong-fixture'})[0] == 401
        code, tools = request('/tools', {'X-Internal-Api-Key': 'fixture-door-key'})
        assert code == 200 and isinstance(tools, list) and len(tools) >= 5
        assert 'search' in {tool['name'] for tool in tools}
        # No database is available in this offline fixture: readiness must fail
        # closed, without converting a running process into authorized readiness.
        code, status = request('/ready')
        assert code == 503, (code, status)
        assert 'fixture-door-key' not in json.dumps(status)
        print(json.dumps({'uid': os.getuid(), 'liveness': 'passed', 'tool_catalog': 'passed',
                          'unauthorized_requests': 'denied', 'ACL_store_outage': 'not-ready'}))
    except Exception:
        log.flush()
        print(Path('/tmp/context-mcp.log').read_text())
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


if __name__ == '__main__':
    main()
