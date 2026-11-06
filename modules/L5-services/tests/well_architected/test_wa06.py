"""WA06 local scenario selectors; deployed client and provider checks remain separate."""

from pathlib import Path

import pytest
import test_wa06_schedule as schedule
from test_wa04 import accepted_review_journey
from test_wa06_publication import authoritative_publication_journey

review_store = schedule.review_store


@pytest.mark.asyncio
async def test_ac_01(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "gateway/tests/internal"))
    from test_well_architected_mcp import shared_client_review_journey

    await shared_client_review_journey(monkeypatch, tmp_path)
    await accepted_review_journey(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_ac_02(review_store):
    await schedule.scheduled_review_journey(review_store)


@pytest.mark.asyncio
async def test_ac_03(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "gateway/tests/internal"))
    from test_well_architected_mcp import shared_client_state_journey

    await shared_client_state_journey(monkeypatch)
    await authoritative_publication_journey(tmp_path)
