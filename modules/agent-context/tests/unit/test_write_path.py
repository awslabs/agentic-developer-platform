"""
Unit tests for Knowledge Layer write path (indexing pipeline).

Test cases W1–W4 from TESTING.md §6. Pure logic tests — no AWS, no cluster.

Validates:
- W1: Unchanged SHA → no enqueue
- W2: Changed SHA → exactly one SQS message
- W3: Clone uses App token for private, anonymous for public
- W4: GitHub token never appears in log output
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fake SQS queue for testing enqueue logic
# ---------------------------------------------------------------------------


@dataclass
class FakeSQSQueue:
    """In-memory SQS queue substitute."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    def send_message(self, body: dict[str, Any]) -> None:
        self.messages.append(body)

    @property
    def message_count(self) -> int:
        return len(self.messages)


@pytest.fixture
def fake_queue() -> FakeSQSQueue:
    return FakeSQSQueue()


# ---------------------------------------------------------------------------
# Fake SHA tracker (Postgres catalog substitute)
# ---------------------------------------------------------------------------


@dataclass
class FakeSHATracker:
    """In-memory SHA tracker simulating the Postgres catalog's last-indexed SHA."""

    _shas: dict[str, str] = field(default_factory=dict)

    def get_last_sha(self, repo_id: str) -> str | None:
        return self._shas.get(repo_id)

    def set_sha(self, repo_id: str, sha: str) -> None:
        self._shas[repo_id] = sha


@pytest.fixture
def fake_sha_tracker() -> FakeSHATracker:
    return FakeSHATracker()


# ---------------------------------------------------------------------------
# Scheduler logic under test
# ---------------------------------------------------------------------------


def should_enqueue(repo_id: str, current_sha: str, tracker: FakeSHATracker) -> bool:
    """Determine if a repo should be enqueued for re-indexing.

    This is the logic that the scheduler (#1346) must implement.
    Returns True if the repo's current SHA differs from the last-indexed SHA.
    """
    last_sha = tracker.get_last_sha(repo_id)
    return last_sha != current_sha


def enqueue_repo(repo_id: str, sha: str, queue: FakeSQSQueue) -> None:
    """Enqueue a single repo for indexing."""
    queue.send_message({"repo_id": repo_id, "sha": sha, "action": "index"})


# ---------------------------------------------------------------------------
# W1: Unchanged SHA → no enqueue
# ---------------------------------------------------------------------------


class TestUnchangedSHASkip:
    """A repo whose SHA hasn't changed should not be enqueued."""

    def test_same_sha_not_enqueued(
        self, fake_sha_tracker: FakeSHATracker, fake_queue: FakeSQSQueue
    ):
        """W1a: Repo with identical SHA produces zero messages."""
        repo_id = "org/unchanged-repo"
        sha = "abc123def456"

        # Simulate previous indexing
        fake_sha_tracker.set_sha(repo_id, sha)

        # Scheduler checks
        if should_enqueue(repo_id, sha, fake_sha_tracker):
            enqueue_repo(repo_id, sha, fake_queue)

        assert fake_queue.message_count == 0

    def test_multiple_unchanged_repos(
        self, fake_sha_tracker: FakeSHATracker, fake_queue: FakeSQSQueue
    ):
        """W1b: Multiple unchanged repos produce zero total messages."""
        repos = [
            ("org/repo-1", "sha-aaa"),
            ("org/repo-2", "sha-bbb"),
            ("org/repo-3", "sha-ccc"),
        ]
        for repo_id, sha in repos:
            fake_sha_tracker.set_sha(repo_id, sha)

        for repo_id, sha in repos:
            if should_enqueue(repo_id, sha, fake_sha_tracker):
                enqueue_repo(repo_id, sha, fake_queue)

        assert fake_queue.message_count == 0


# ---------------------------------------------------------------------------
# W2: Changed SHA → exactly one SQS message
# ---------------------------------------------------------------------------


class TestChangedSHAEnqueue:
    """A repo with a new SHA should produce exactly one SQS message."""

    def test_new_sha_enqueued(self, fake_sha_tracker: FakeSHATracker, fake_queue: FakeSQSQueue):
        """W2a: Changed SHA produces exactly one message."""
        repo_id = "org/updated-repo"
        old_sha = "old-sha-111"
        new_sha = "new-sha-222"

        fake_sha_tracker.set_sha(repo_id, old_sha)

        if should_enqueue(repo_id, new_sha, fake_sha_tracker):
            enqueue_repo(repo_id, new_sha, fake_queue)

        assert fake_queue.message_count == 1
        assert fake_queue.messages[0]["repo_id"] == repo_id
        assert fake_queue.messages[0]["sha"] == new_sha

    def test_never_indexed_repo_enqueued(
        self, fake_sha_tracker: FakeSHATracker, fake_queue: FakeSQSQueue
    ):
        """W2b: A repo that was never indexed (no SHA in tracker) is enqueued."""
        repo_id = "org/brand-new-repo"
        sha = "first-sha-aaa"

        if should_enqueue(repo_id, sha, fake_sha_tracker):
            enqueue_repo(repo_id, sha, fake_queue)

        assert fake_queue.message_count == 1

    def test_only_changed_repos_enqueued(
        self, fake_sha_tracker: FakeSHATracker, fake_queue: FakeSQSQueue
    ):
        """W2c: Among N repos, only those with changed SHAs produce messages."""
        repos = [
            ("org/unchanged", "sha-same", "sha-same"),  # unchanged
            ("org/changed-1", "sha-old-1", "sha-new-1"),  # changed
            ("org/changed-2", "sha-old-2", "sha-new-2"),  # changed
        ]
        for repo_id, old_sha, _ in repos:
            fake_sha_tracker.set_sha(repo_id, old_sha)

        for repo_id, _, current_sha in repos:
            if should_enqueue(repo_id, current_sha, fake_sha_tracker):
                enqueue_repo(repo_id, current_sha, fake_queue)

        assert fake_queue.message_count == 2
        enqueued_repos = {m["repo_id"] for m in fake_queue.messages}
        assert enqueued_repos == {"org/changed-1", "org/changed-2"}


# ---------------------------------------------------------------------------
# W3: Clone auth — App token for private, anonymous for public
# ---------------------------------------------------------------------------


def build_clone_url(repo_id: str, is_private: bool, app_token: str | None) -> str:
    """Build the git clone URL for a repository.

    This is the logic that the single-fetch clone (#1347) must implement.
    - Private repos: use the GitHub App installation token in the URL
    - Public repos: clone anonymously (no token)
    """
    if is_private:
        if not app_token:
            raise ValueError(f"Private repo {repo_id} requires an App token")
        return f"https://x-access-token:{app_token}@github.com/{repo_id}.git"
    return f"https://github.com/{repo_id}.git"


class TestCloneAuth:
    """Clone URL correctly uses tokens for private repos and goes anonymous for public."""

    def test_private_repo_uses_token(self):
        """W3a: Private repo clone URL includes the App token."""
        token = "ghs_" + "a" * 36
        url = build_clone_url("org/private-repo", is_private=True, app_token=token)

        assert f"x-access-token:{token}@" in url
        assert "org/private-repo.git" in url

    def test_public_repo_no_token(self):
        """W3b: Public repo clone URL has no token."""
        url = build_clone_url("oss/public-lib", is_private=False, app_token=None)

        assert "x-access-token" not in url
        assert "github.com/oss/public-lib.git" in url

    def test_private_repo_without_token_raises(self):
        """W3c: Private repo without a token raises ValueError."""
        with pytest.raises(ValueError, match="requires an App token"):
            build_clone_url("org/private-repo", is_private=True, app_token=None)


# ---------------------------------------------------------------------------
# W4: Token never in logs
# ---------------------------------------------------------------------------


# Regex pattern for GitHub tokens (App installation tokens, PATs, etc.)
_TOKEN_PATTERNS = [
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),  # GitHub App installation token
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub PAT (classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # Fine-grained PAT
    re.compile(r"x-access-token:[^\s@]+@"),  # Token in URL
]


def sanitize_for_logging(message: str) -> str:
    """Sanitize a message to remove GitHub tokens before logging.

    This is the function that #1347 must use for all log output.
    """
    sanitized = message
    for pattern in _TOKEN_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


class TestTokenNotInLogs:
    """GitHub tokens must never appear in any log output."""

    def test_app_token_redacted(self):
        """W4a: GitHub App installation token is redacted in log messages."""
        token = "ghs_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        message = f"Cloning repo with token {token}"

        sanitized = sanitize_for_logging(message)

        assert token not in sanitized
        assert "[REDACTED]" in sanitized

    def test_pat_redacted(self):
        """W4b: GitHub PAT is redacted in log messages."""
        token = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        message = f"Using PAT: {token}"

        sanitized = sanitize_for_logging(message)

        assert token not in sanitized

    def test_url_with_token_redacted(self):
        """W4c: Clone URL containing token has the token portion redacted."""
        token = "ghs_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        url = f"https://x-access-token:{token}@github.com/org/repo.git"

        sanitized = sanitize_for_logging(url)

        assert token not in sanitized
        assert "github.com" in sanitized

    def test_safe_message_unchanged(self):
        """W4d: Messages without tokens pass through unchanged."""
        message = "Cloning public repo https://github.com/oss/lib.git"

        sanitized = sanitize_for_logging(message)

        assert sanitized == message

    def test_log_handler_integration(self, caplog):
        """W4e: Verify logging through a filter never leaks tokens."""
        token = "ghs_AbCdEfGhIjKlMnOpQrStUvWxYz012345"

        class TokenFilter(logging.Filter):
            def filter(self, record):
                # Sanitize both the format string and any args
                record.msg = sanitize_for_logging(str(record.msg))
                if record.args:
                    if isinstance(record.args, dict):
                        record.args = {
                            k: sanitize_for_logging(str(v)) for k, v in record.args.items()
                        }
                    else:
                        record.args = tuple(sanitize_for_logging(str(a)) for a in record.args)
                return True

        logger = logging.getLogger("test.token_filter")
        logger.addFilter(TokenFilter())

        with caplog.at_level(logging.INFO, logger="test.token_filter"):
            logger.info("Cloning with %s", token)

        # The token should not appear anywhere in captured logs
        for record in caplog.records:
            assert token not in record.getMessage()
