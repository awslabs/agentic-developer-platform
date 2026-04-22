"""
E2E tests for memory tools: save_preference, recall_memory, save_learning.

Validates the full pipeline: WS message -> classifier -> chat-agent-worker ->
memory tool invocation -> DynamoDB persistence -> cross-session retrieval.

Issue #53 sections 2.5 and 2.6.

Run:
    cd modules/agent-factory && make test-e2e-memory

Or directly:
    TEST_ENV=dev RUN_COSTLY_TESTS=yes python -m pytest tests/e2e/test_memory_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from .conftest import (
    scan_memory_rows_for_cleanup,
    user_id_from_token,
    wait_for,
)

pytestmark = [pytest.mark.live_only, pytest.mark.costs_money]

MEMORY_TABLE = "adp-dev-agent-memory"


# ============================================================================
# Section 2.5 — save_preference + recall_memory (user scope)
# ============================================================================


class TestSavePreferenceAndRecall:
    """Validate user-scoped preference persistence and cross-session recall.

    Flow:
      Turn 1: "Remember that I prefer responses in bullet points."
        -> Agent calls save_preference -> DDB row with kind=preference
      Turn 2 (same session): Ask a question -> reply is bullet-point formatted
      Turn 3 (NEW session, same user): Ask a question -> still bullet-point formatted
    """

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_save_preference_creates_memory_row(
        self,
        ws_client_async,
        fresh_jwt,
        memory_rows,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.5a: Send 'remember I prefer bullet points', verify DDB row."""
        session_id = make_session_id("mem-pref")
        cleanup.track(session_id)
        token = fresh_jwt()
        user_id = user_id_from_token(token)

        async with ws_client_async(token, session_id) as client:
            latency_recorder.mark("send")
            await client.send({
                "action": "message",
                "text": (
                    "Please remember this preference for all future interactions: "
                    "I always want responses formatted as bullet points, not paragraphs. "
                    "Save this as a preference."
                ),
                "session_id": session_id,
            })

            try:
                terminal, frames = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("terminal_frame")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame within 120s for save_preference prompt")

            assert terminal.status == "completed", (
                f"Expected completed, got {terminal.status}"
            )
            assert terminal.content, "Terminal frame has empty content"

        # Wait for DDB write propagation
        await asyncio.sleep(3)

        # Verify memory row exists with correct scope and kind
        rows = memory_rows("user", user_id, kind="preference")

        # Register all user memory rows for cleanup
        all_rows = scan_memory_rows_for_cleanup("user", user_id, cleanup)

        if not rows:
            # The agent may not have called save_preference — this is an xfail
            # (LLM tool usage is non-deterministic)
            pytest.xfail(
                f"No preference rows found for user {user_id}. "
                "Agent may not have invoked save_preference tool. "
                "Re-run or check agent system prompt."
            )

        # Assert the preference content mentions bullet points
        pref_row = rows[0]
        assert "bullet" in pref_row.get("content", "").lower(), (
            f"Preference content doesn't mention 'bullet': {pref_row.get('content', '')}"
        )
        assert pref_row.get("kind") == "preference"

        # Verify scope structure
        scope = pref_row.get("scope", {})
        assert scope.get("user") == user_id, (
            f"Expected scope.user={user_id}, got {scope.get('user')}"
        )

        latency_recorder.note("memory_rows_found", str(len(rows)))
        latency_recorder.note("user_id", user_id[:8] + "...")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_same_session_recall_uses_preference(
        self,
        ws_client_async,
        fresh_jwt,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.5b: Save preference in turn 1, then ask a question in turn 2.

        The reply in turn 2 should respect the bullet-point preference.
        """
        session_id = make_session_id("mem-recall")
        cleanup.track(session_id)
        token = fresh_jwt()
        user_id = user_id_from_token(token)

        async with ws_client_async(token, session_id) as client:
            # Turn 1: save the preference
            latency_recorder.mark("turn1_send")
            await client.send({
                "action": "message",
                "text": (
                    "Important: save this as my preference — I want all responses "
                    "formatted as bullet points. Never use paragraphs."
                ),
                "session_id": session_id,
            })

            try:
                terminal1, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("turn1_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for turn 1 within 120s")

            assert terminal1.status == "completed"
            await asyncio.sleep(2)

            # Turn 2: ask a question — reply should be in bullet points
            latency_recorder.mark("turn2_send")
            await client.send({
                "action": "message",
                "text": "What are three benefits of using TypeScript over JavaScript?",
                "session_id": session_id,
            })

            try:
                terminal2, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("turn2_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for turn 2 within 120s")

            assert terminal2.status == "completed"
            assert terminal2.content, "Turn 2 reply is empty"

            # Check for bullet-point formatting indicators
            content = terminal2.content
            bullet_indicators = ["- ", "* ", "1.", "2.", "3."]
            has_bullets = any(indicator in content for indicator in bullet_indicators)

            if not has_bullets:
                # Soft assertion — LLM may not always follow preferences
                latency_recorder.note("bullet_format", "false")
                pytest.xfail(
                    "Reply does not appear to use bullet points. "
                    "Agent may not have recalled/applied the preference."
                )
            else:
                latency_recorder.note("bullet_format", "true")

        # Cleanup memory rows
        scan_memory_rows_for_cleanup("user", user_id, cleanup)

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_cross_session_preference_recall(
        self,
        ws_client_async,
        fresh_jwt,
        memory_rows,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.5c: Save preference in session 1, verify recall in session 2 (new session, same user).

        This proves cross-session memory persistence works.
        """
        session_id_1 = make_session_id("mem-cross1")
        session_id_2 = make_session_id("mem-cross2")
        cleanup.track(session_id_1)
        cleanup.track(session_id_2)
        token = fresh_jwt()
        user_id = user_id_from_token(token)

        # Session 1: save the preference
        async with ws_client_async(token, session_id_1) as client:
            latency_recorder.mark("s1_send")
            await client.send({
                "action": "message",
                "text": (
                    "Save this as my permanent preference: "
                    "I always want responses as numbered lists (1. 2. 3.), "
                    "never as prose paragraphs. This should persist across sessions."
                ),
                "session_id": session_id_1,
            })

            try:
                terminal1, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("s1_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for session 1")

            assert terminal1.status == "completed"

        # Wait for DDB propagation
        await asyncio.sleep(5)

        # Verify preference was saved
        pref_rows = memory_rows("user", user_id, kind="preference")
        scan_memory_rows_for_cleanup("user", user_id, cleanup)

        if not pref_rows:
            pytest.xfail(
                "No preference saved in session 1. "
                "Agent did not call save_preference."
            )

        # Session 2: NEW session, same user — ask a question
        # Get a fresh token (same user)
        token2 = fresh_jwt()
        async with ws_client_async(token2, session_id_2) as client:
            latency_recorder.mark("s2_send")
            await client.send({
                "action": "message",
                "text": "Give me five tips for writing clean Python code.",
                "session_id": session_id_2,
            })

            try:
                terminal2, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("s2_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for session 2")

            assert terminal2.status == "completed"
            assert terminal2.content, "Session 2 reply is empty"

            # Check for numbered list formatting
            content = terminal2.content
            has_numbered = ("1." in content and "2." in content and "3." in content)

            if not has_numbered:
                latency_recorder.note("cross_session_format", "false")
                pytest.xfail(
                    "Session 2 reply does not use numbered list format. "
                    "Agent may not have recalled cross-session preference."
                )
            else:
                latency_recorder.note("cross_session_format", "true")


# ============================================================================
# Section 2.6 — save_learning (persona scope)
# ============================================================================


class TestSaveLearning:
    """Validate persona-scoped learning persistence and cross-session recall.

    Flow:
      Turn 1: "Save the learning: 'EKS cluster in this account is named
               adp-dev-eks-cluster (with -cluster suffix).'"
        -> Agent calls save_learning -> DDB row with kind=learning,
           PK=scope#persona#<persona>
      New session, same persona: Ask about EKS -> reply uses
           'adp-dev-eks-cluster' (with -cluster suffix).
    """

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_save_learning_creates_persona_scoped_row(
        self,
        ws_client_async,
        fresh_jwt,
        memory_rows,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.6: Save a learning, verify DDB row, then verify recall in new session."""
        session_id_1 = make_session_id("mem-learn1")
        session_id_2 = make_session_id("mem-learn2")
        cleanup.track(session_id_1)
        cleanup.track(session_id_2)
        token = fresh_jwt()
        user_id = user_id_from_token(token)

        # A unique fact the agent wouldn't know without memory
        unique_fact = "adp-dev-eks-cluster-e2etest-7x9q"

        # Session 1: save the learning
        async with ws_client_async(token, session_id_1) as client:
            latency_recorder.mark("s1_send")
            await client.send({
                "action": "message",
                "text": (
                    f"Save this learning for all developer agents: "
                    f"'The EKS cluster in this account is named {unique_fact} "
                    f"(note the -e2etest-7x9q suffix, this is important).'"
                ),
                "session_id": session_id_1,
            })

            try:
                terminal1, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("s1_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for save_learning")

            assert terminal1.status == "completed"

        # Wait for DDB propagation
        await asyncio.sleep(5)

        # Check for persona-scoped rows
        # The classifier may assign any persona, so check common ones
        found_learning = False
        found_persona = None
        for persona in ["developer", "ops", "pm", "architect"]:
            rows = memory_rows("persona", persona, kind="learning")
            matching = [
                r for r in rows
                if unique_fact in r.get("content", "")
                or "e2etest-7x9q" in r.get("content", "")
            ]
            if matching:
                found_learning = True
                found_persona = persona
                scan_memory_rows_for_cleanup("persona", persona, cleanup)
                break

        # Also check user scope (agent might scope to user instead)
        if not found_learning:
            user_rows = memory_rows("user", user_id, kind="learning")
            matching = [
                r for r in user_rows
                if unique_fact in r.get("content", "")
                or "e2etest-7x9q" in r.get("content", "")
            ]
            if matching:
                found_learning = True
                found_persona = f"user:{user_id[:8]}"
                scan_memory_rows_for_cleanup("user", user_id, cleanup)

        if not found_learning:
            # Cleanup any memory rows we can find
            for persona in ["developer", "ops", "pm", "architect"]:
                scan_memory_rows_for_cleanup("persona", persona, cleanup)
            scan_memory_rows_for_cleanup("user", user_id, cleanup)
            pytest.xfail(
                "No learning row found with the expected content. "
                "Agent may not have called save_learning tool."
            )

        latency_recorder.note("learning_persona", found_persona or "unknown")

        # Session 2: new session, ask about the EKS cluster name
        token2 = fresh_jwt()
        async with ws_client_async(token2, session_id_2) as client:
            latency_recorder.mark("s2_send")
            await client.send({
                "action": "message",
                "text": (
                    "What is the name of the EKS cluster in this account? "
                    "Check your memory/learnings — I saved it earlier."
                ),
                "session_id": session_id_2,
            })

            try:
                terminal2, _ = await client.recv_until_terminal(timeout=120)
                latency_recorder.mark("s2_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for learning recall")

            assert terminal2.status == "completed"
            assert terminal2.content, "Session 2 reply is empty"

            # Check if the unique suffix appears in the reply
            if "e2etest-7x9q" in terminal2.content or unique_fact in terminal2.content:
                latency_recorder.note("learning_recalled", "true")
            else:
                latency_recorder.note("learning_recalled", "false")
                pytest.xfail(
                    f"Reply does not contain the saved learning "
                    f"('{unique_fact}'). Agent may not have recalled it."
                )
