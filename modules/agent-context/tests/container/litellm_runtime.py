"""Real offline LiteLLM HTTP/auth/proxy acceptance under the image user."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    assert Path.home() == Path('/home/appuser')
    for path in ('/usr/local/bin/litellm', '/config/config.yaml', '/etc/passwd'):
        try:
            with open(path, 'ab'):
                pass
        except PermissionError:
            pass
        except OSError as exc:
            assert exc.errno == 30
        else:
            raise AssertionError('protected path writable: ' + path)
    received = []
    provider_status = [200]

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers.get('Authorization'), body))
            if provider_status[0] != 200:
                self.send_response(provider_status[0])
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"fixture unavailable","type":"server_error"}}')
                return
            payload = json.dumps({'id': 'chatcmpl-fixture', 'object': 'chat.completion',
                                  'created': 1, 'model': 'fixture', 'choices': [
                                      {'index': 0, 'message': {'role': 'assistant', 'content': 'fixture-ok'},
                                       'finish_reason': 'stop'}],
                                  'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 18081), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log = open('/tmp/litellm.log', 'w')
    process = subprocess.Popen(['litellm', '--config', '/config/config.yaml', '--port', '4000',
                                '--host', '127.0.0.1'], stdout=log, stderr=log)
    try:
        ready = False
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError(Path('/tmp/litellm.log').read_text())
            try:
                with urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.5)
        assert ready, Path('/tmp/litellm.log').read_text()
        for headers in ({}, {'Authorization': 'Bearer sk-wrong-fixture'}):
            request = urllib.request.Request('http://127.0.0.1:4000/v1/models', headers=headers)
            try:
                urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as exc:
                body = json.load(exc)
                if headers:
                    # Upstream explicitly refuses non-master keys without a DB.
                    assert exc.code == 400 and body['error']['type'] == 'no_db_connection', (exc.code, body)
                else:
                    assert exc.code == 401, (exc.code, body)
            else:
                raise AssertionError('unauthorized model access admitted')
        assert not received
        payload = json.dumps({'model': 'fixture-model', 'messages': [
            {'role': 'user', 'content': 'synthetic request'}]}).encode()
        request = urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions', data=payload,
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer sk-fixture-master-key'})
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        assert result['choices'][0]['message']['content'] == 'fixture-ok'
        assert len(received) == 1
        assert received[0][0] == '/v1/chat/completions'
        assert received[0][1] == 'Bearer fixture-provider-key'
        assert received[0][2]['messages'][0]['content'] == 'synthetic request'
        before = len(received)
        malformed = urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions', data=b'not-json',
                                            headers={'Content-Type': 'application/json',
                                                     'Authorization': 'Bearer sk-fixture-master-key'})
        try:
            urllib.request.urlopen(malformed, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, (exc.code, exc.read().decode())
            assert 'error' in json.load(exc)
        else:
            raise AssertionError('malformed request admitted')
        assert len(received) == before
        provider_status[0] = 503
        try:
            urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503, (exc.code, exc.read().decode())
            assert 'error' in json.load(exc)
        else:
            raise AssertionError('unavailable provider reported success')
        print(json.dumps({'uid': os.getuid(), 'startup': 'passed', 'auth_negative': 'passed',
                          'real_proxy_roundtrip': 'passed', 'root_and_config_writes': 'denied',
                          'malformed_request': 'denied', 'provider_outage': '503'}))
    except Exception:
        log.flush()
        print(Path('/tmp/litellm.log').read_text())
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    main()
