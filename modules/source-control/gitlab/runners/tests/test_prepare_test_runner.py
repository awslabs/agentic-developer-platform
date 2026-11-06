"""Daemon-free acquisition of the exact release-locked Runner binary."""
import hashlib
import importlib.util
import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_test_runner.py"
spec = importlib.util.spec_from_file_location("prepare_test_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
BINARY = b"offline synthetic runner fixture"
DIGEST = hashlib.sha256(BINARY).hexdigest()
SOURCE = "https://gitlab-runner-downloads.s3.amazonaws.com/v19.5.2/binaries/gitlab-runner-linux-amd64"


class Response(io.BytesIO):
    status = 200


class Downloader:
    def __init__(self, data):
        self.data = data
        self.source = None
        self.timeout = None
        self.handlers = None

    def build_opener(self, *handlers):
        self.handlers = handlers
        return self

    def open(self, source, timeout):
        self.source = source
        self.timeout = timeout
        return Response(self.data)


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(runner, "TEST_BINARY_SHA256", DIGEST)
    lock = {"chart": {"runner_version": "19.5.2"}, "manager_image": runner.MANAGER_IMAGE,
            "test_runner_binary": {"source": SOURCE, "sha256": DIGEST}}
    downloader = Downloader(BINARY)
    monkeypatch.setattr(runner.urllib.request, "build_opener", downloader.build_opener)
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, b"Version:      19.5.2\nOS/Arch:      linux/amd64\n"))
    return lock, downloader


def test_exact_binary_installs_exclusively_after_verification(tmp_path, pinned):
    lock, downloader = pinned
    output = tmp_path / "runner"
    receipt = runner.prepare(lock, output)
    assert receipt["binary_sha256"] == DIGEST
    assert output.read_bytes() == BINARY
    assert output.stat().st_mode & 0o777 == 0o700
    assert downloader.source == SOURCE and downloader.timeout == 30
    assert isinstance(downloader.handlers[0], runner.NoRedirect)
    with pytest.raises(ValueError, match="output_exists"):
        runner.prepare(lock, output)
    assert output.read_bytes() == BINARY


@pytest.mark.parametrize("change", [
    lambda lock: lock["test_runner_binary"].update(sha256="0" * 64),
    lambda lock: lock["test_runner_binary"].update(source="https://other.example.test/runner"),
    lambda lock: lock["chart"].update(runner_version="19.5.3"),
    lambda lock: lock.update(manager_image="gitlab/gitlab-runner:latest"),
])
def test_missing_or_changed_lock_refused_before_download(tmp_path, pinned, change):
    lock, downloader = pinned
    change(lock)
    with pytest.raises(ValueError, match="not_pinned"):
        runner.prepare(lock, tmp_path / "runner")
    assert downloader.source is None
    assert not (tmp_path / "runner").exists()


@pytest.mark.parametrize("data,limit,error", [
    (b"incorrect", 1024, "checksum_wrong"),
    (b"", 1024, "checksum_wrong"),
    (BINARY, 8, "too_large"),
])
def test_truncated_corrupt_or_oversize_download_never_installs(tmp_path, pinned, monkeypatch, data, limit, error):
    lock, downloader = pinned
    downloader.data = data
    monkeypatch.setattr(runner, "MAX_BINARY_BYTES", limit)
    with pytest.raises(ValueError, match=error):
        runner.prepare(lock, tmp_path / "runner")
    assert not (tmp_path / "runner").exists()


def test_redirect_refused():
    downloader = runner.NoRedirect()
    with pytest.raises(ValueError, match="redirect_refused"):
        downloader.redirect_request(None, None, 302, "Found", {}, "https://other.example.test/runner")


def test_wrong_version_never_installs(tmp_path, pinned, monkeypatch):
    lock, _ = pinned
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, b"Version:      19.5.3\nOS/Arch:      linux/amd64\n"))
    with pytest.raises(ValueError, match="version_wrong"):
        runner.prepare(lock, tmp_path / "runner")
    assert not (tmp_path / "runner").exists()


def test_missing_binary_pin_fails_before_download(tmp_path, pinned):
    lock, downloader = pinned
    del lock["test_runner_binary"]
    with pytest.raises(KeyError):
        runner.prepare(lock, tmp_path / "runner")
    assert downloader.source is None
    assert not (tmp_path / "runner").exists()
