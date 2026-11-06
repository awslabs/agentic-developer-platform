import pytest

from tests.e2e.tools import runner


class Observer:
    def __init__(self):
        self.count = 0
        self.terminated = False

    def provider_calls(self):
        return self.count

    def session_status(self):
        return "TERMINATED" if self.terminated else "READY"


class Driver:
    isolated = True
    authorized = True

    def __init__(self, observer, *, escape=False):
        self.observer = observer
        self.escape = escape
        self.cleaned = False

    def foreign(self):
        return {"status": 404}

    def traversal(self):
        return {"status": 422}

    def expired(self):
        return {"status": 403}

    def symlink(self):
        self.observer.count += 1
        return {"status": "confirmed" if self.escape else "failed"}

    def nonzero(self):
        self.observer.count += 2
        return {"status": "failed", "exitCode": 2}

    def cleanup(self):
        self.cleaned = True
        self.observer.terminated = True


def test_interpreter_ac02_denials_and_nonzero_are_independently_observed():
    observer = Observer()
    driver = Driver(observer)
    result = runner.interpreter_ac02(driver, observer)
    assert result == {"denials": {"foreign": 404, "traversal": 422, "expired": 403},
                      "nonzero_exit": 2, "observed_provider_calls": 3}
    assert driver.cleaned and observer.session_status() == "TERMINATED"
    assert runner.select_scenarios("SCN-8070-02")[0].action == "interpreter_ac02"


def test_interpreter_ac02_escape_fails_and_still_cleans_up():
    observer = Observer()
    driver = Driver(observer, escape=True)
    with pytest.raises(ValueError, match="symlink"):
        runner.interpreter_ac02(driver, observer)
    assert driver.cleaned and observer.session_status() == "TERMINATED"


def test_interpreter_ac02_rejects_missing_authorized_fixture():
    observer = Observer()
    driver = Driver(observer)
    driver.authorized = False
    with pytest.raises(runner.FixtureUnavailable):
        runner.interpreter_ac02(driver, observer)
    assert observer.count == 0
