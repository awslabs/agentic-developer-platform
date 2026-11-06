"""Browser diagnostics stay in the requested evidence root with private modes."""
from pathlib import Path
from unittest.mock import Mock

from tests.e2e.chat.helpers import take_failure_screenshot


def test_screenshots_are_private_unique_and_confined(tmp_path, monkeypatch):
    directory = tmp_path / 'screenshots'
    monkeypatch.setenv('E2E_SCREENSHOT_DIR', str(directory))
    page = Mock()
    page.screenshot.side_effect = lambda *, path: Path(path).write_bytes(b'png')
    first = Path(take_failure_screenshot(page, '../../test secret:name'))
    second = Path(take_failure_screenshot(page, '../../test secret:name'))
    assert first != second
    assert first.parent == second.parent == directory
    assert first.read_bytes() == second.read_bytes() == b'png'
    assert first.stat().st_mode & 0o777 == second.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
