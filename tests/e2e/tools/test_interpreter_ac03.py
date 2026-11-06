import base64
import hashlib

import pytest

from tests.e2e.tools import runner


class Observer:
    def __init__(self):
        self.executions = 0
        self.terminated = False

    def execute_calls(self):
        return self.executions

    def session_status(self):
        return "TERMINATED" if self.terminated else "READY"


class Driver:
    isolated = True
    authorized = True

    def __init__(self, observer, *, replay=False, deadline=400):
        self.observer = observer
        self.replay = replay
        self.deadline = deadline
        self.cleaned = False

    def lose_execute_reply(self):
        self.observer.executions += 1
        return {"status": "unknown", "operation_ref": "original-ref"}

    def recover_same_reference(self, reference):
        assert reference == "original-ref"
        if self.replay:
            self.observer.executions += 1
        return {"status": "confirmed", "operation_ref": reference, "stdout": "42\n", "exitCode": 0}

    def read_owned_file(self):
        return {"data_base64": base64.b64encode(b"42\n").decode(),
                "sha256": hashlib.sha256(b"42\n").hexdigest()}

    def cleanup(self):
        self.cleaned = True
        self.observer.terminated = True
        return {"status": "confirmed", "elapsed_seconds": self.deadline}


def test_interpreter_ac03_recovers_original_reference_and_checks_provider_stop():
    observer = Observer()
    driver = Driver(observer)
    result = runner.interpreter_ac03(driver, observer)
    assert result["observed_execute_calls"] == 1 and result["cleanup_seconds"] == 400
    assert driver.cleaned and observer.session_status() == "TERMINATED"
    assert runner.select_scenarios("SCN-8070-03")[0].action == "interpreter_ac03"


@pytest.mark.parametrize("replay,deadline,error", [(True, 400, "replayed"), (False, 601, "600 seconds")])
def test_interpreter_ac03_rejects_replay_or_late_stop(replay, deadline, error):
    observer = Observer()
    driver = Driver(observer, replay=replay, deadline=deadline)
    with pytest.raises(ValueError, match=error):
        runner.interpreter_ac03(driver, observer)
    assert driver.cleaned


def test_interpreter_ac03_requires_isolated_authority():
    observer = Observer()
    driver = Driver(observer)
    driver.isolated = False
    with pytest.raises(runner.FixtureUnavailable):
        runner.interpreter_ac03(driver, observer)
    assert observer.executions == 0
