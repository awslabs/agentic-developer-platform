"""Expose retained offline render paths even when pytest captures passing output."""


def pytest_terminal_summary(terminalreporter):
    for report in terminalreporter.stats.get("passed", []):
        for line in report.capstdout.splitlines():
            if line.startswith("runner_rendered_fixture_path="):
                terminalreporter.write_line(line)
