"""Real GitHub qualification using an operator credential and fixture Task authority."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from src.agentauth import task_repository_publication as publication
from src.agentauth.task_repository_source import TaskGitHubSource


class LiveGitHub:
    def __init__(self, monkeypatch):
        self.binding = json.loads(Path(os.environ["ADP_CODEX_LIVE_GITHUB_BINDING"]).read_text())
        if (set(self.binding) != {"repository", "repository_id", "base_branch", "source_revision"}
                or self.binding["repository"] != "aws-e/adp"
                or self.binding["repository_id"] != "1186991269"
                or not re.fullmatch(r"qualification/5433-developer-[0-9-]+", self.binding["base_branch"])
                or not re.fullmatch(r"[a-f0-9]{40}", self.binding["source_revision"])):
            raise ValueError("Live GitHub qualification binding invalid")
        token_file = Path(os.environ["ADP_CODEX_LIVE_GITHUB_TOKEN_FILE"])
        if token_file.stat().st_mode & 0o077:
            raise ValueError("Live GitHub credential file must be private")
        self._token = token_file.read_text().strip()
        if not self._token:
            raise ValueError("Live GitHub credential unavailable")

        async def connection(*, db, tenant, binding):
            self.check_binding(binding)
            return 123

        async def credential(**kwargs):
            if kwargs["repository"] != self.binding["repository"]:
                raise ValueError("Live GitHub credential scope differs")
            return self._token

        # Only the qualification auth seam is replaced; real adapters enforce
        # source/tree identity, branch fencing, mutation and final observation.
        monkeypatch.setattr(publication, "authorize_source_connection", connection)
        monkeypatch.setattr(publication, "installation_token", credential)

    def check_binding(self, binding):
        if any(binding.get(key) != self.binding[key] for key in ("repository", "repository_id", "base_branch")):
            raise ValueError("Live GitHub repository scope differs")

    async def source(self, binding, reauthorize):
        self.check_binding(binding)
        async with TaskGitHubSource(binding=binding, token=self._token, reauthorize=reauthorize) as provider:
            result = await provider.download()
        if result.commit_sha != self.binding["source_revision"]:
            raise ValueError("Live qualification base moved")
        return result

    async def publish(self, **kwargs):
        self.check_binding(kwargs["frozen"]["binding"])
        if kwargs["manifest"]["source_revision"] != self.binding["source_revision"]:
            raise ValueError("Live qualification source differs")
        kwargs["title"] = "[Qualification #5433] " + kwargs["title"]
        kwargs["body"] += (
            "\n\nQualification: real GPT inference, isolated Docker validation and GitHub publication. "
            "Task identity/ledger and installation authorization use local fixtures with an operator credential. "
            "This targets a disposable qualification branch and does not establish production readiness."
        )
        return await publication.publish_task_change(db=None, **kwargs)

    async def observe(self, **kwargs):
        self.check_binding(kwargs["frozen"]["binding"])
        return await publication.observe_task_change(db=None, **kwargs)
