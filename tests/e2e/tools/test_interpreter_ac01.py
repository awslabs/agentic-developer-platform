import base64
import hashlib
import json

import pytest

from tests.e2e.tools import runner


class Observer:
    def __init__(self):
        self.count = 0
        self.status = "READY"

    def provider_calls(self):
        return self.count

    def session_status(self):
        return self.status


class Client:
    def __init__(self, observer, content=b"42\n"):
        self.observer = observer
        self.content = content
        self.calls = []

    def request(self, method, params):
        assert method == "tools/call"
        name = params["name"]
        arguments = params["arguments"]
        self.calls.append((name, arguments))
        operation_ref = f"ref-{len(self.calls)}"
        execution_id = "owned-execution"
        if name == "adp___artifact_read":
            receipt = {"data_base64": base64.b64encode(self.content).decode(),
                       "sha256": hashlib.sha256(self.content).hexdigest()}
        else:
            self.observer.count += 1
            if name == "code_interpreter___start":
                value = {"session_id": "owned-session"}
            elif name == "code_interpreter___execute":
                value = {"provider_id": "task-one"}
            elif name == "code_interpreter___result":
                value = {"stdout": "42\n", "exitCode": 0}
                value = {"result": value}
            elif name == "code_interpreter___file":
                value = {"artifact": {"content_sha256": hashlib.sha256(self.content).hexdigest()}}
            else:
                assert name == "code_interpreter___close"
                self.observer.status = "TERMINATED"
                value = {"termination_confirmed": True}
            receipt = {"status": "confirmed", "operation_ref": operation_ref, "execution_id": execution_id,
                       "result": {"result": {"result": value}}}
        return {"isError": False, "content": [{"type": "text", "text": json.dumps(receipt)}]}


def test_interpreter_ac01_checks_exact_bytes_and_confirms_provider_close():
    observer = Observer()
    client = Client(observer)
    observation = runner.interpreter_ac01(client, {"path": "/tmp/adp-8070-result.txt"}, observer)
    assert observation["file_sha256"] == hashlib.sha256(b"42\n").hexdigest()
    assert observation["provider_calls"] == 5
    assert observer.status == "TERMINATED"
    assert [name for name, _ in client.calls] == [
        "code_interpreter___start", "code_interpreter___execute", "code_interpreter___result",
        "code_interpreter___file", "adp___artifact_read", "code_interpreter___close"]
    assert "print(6*7)" in client.calls[1][1]["code"]
    assert all(arguments["execution_ref"] == "ref-1"
               for name, arguments in client.calls[1:4])


def test_interpreter_ac01_wrong_bytes_fail_but_still_close():
    observer = Observer()
    client = Client(observer, content=b"43\n")
    with pytest.raises(ValueError, match="file bytes"):
        runner.interpreter_ac01(client, {"path": "/tmp/adp-8070-result.txt"}, observer)
    assert observer.status == "TERMINATED"


def test_interpreter_ac01_requires_isolated_fixture():
    with pytest.raises(runner.FixtureUnavailable):
        runner.interpreter_ac01(Client(Observer()), {}, Observer())
    assert [case.identifier for case in runner.select_scenarios("SCN-8070-01")] == ["SCN-8070-01"]
