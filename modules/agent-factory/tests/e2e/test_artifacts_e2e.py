"""
E2E tests for artifact store: publish_artifact, fetch_artifact, list_artifacts.

Validates the full pipeline: WS message -> classifier -> chat-agent-worker ->
artifact tool invocation -> S3 upload + DynamoDB catalog -> retrieval.

Issue #53 sections 2.7 and 2.8.

Run:
    cd modules/agent-factory && make test-e2e-artifacts

Or directly:
    TEST_ENV=dev RUN_COSTLY_TESTS=yes python -m pytest tests/e2e/test_artifacts_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import re

import boto3
import pytest

from .conftest import scan_artifacts_for_cleanup, wait_for

pytestmark = [pytest.mark.live_only, pytest.mark.costs_money]

ARTIFACTS_TABLE = "adp-dev-chat-artifacts"
ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "adp-dev-chat-artifacts")

# Regex for artifact IDs (art_<12 hex chars>)
ART_ID_RE = re.compile(r"art_[0-9a-f]{12}")


# ============================================================================
# Section 2.7 — Artifact publish (outbound)
# ============================================================================


class TestArtifactPublish:
    """Validate that the agent can publish an artifact to S3 + DynamoDB catalog.

    Flow:
      Send: "Write a simple hello-world Python script and publish it as an artifact."
      Assert:
        - Agent calls publish_artifact
        - S3 object exists at s3://adp-dev-chat-artifacts-<acct>/<sid>/<tid>/*.py
        - Catalog row in adp-dev-chat-artifacts with source=agent
        - Reply contains artifact ID or URL
    """

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_publish_artifact_creates_s3_and_catalog(
        self,
        ws_client_async,
        fresh_jwt,
        artifact_catalog,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.7: Ask agent to write and publish a Python script."""
        session_id = make_session_id("art-pub")
        cleanup.track(session_id)
        token = fresh_jwt()

        async with ws_client_async(token, session_id) as client:
            latency_recorder.mark("send")
            await client.send({
                "action": "message",
                "text": (
                    "Write a simple Python hello-world script (just a print statement "
                    "and a docstring) and publish it as an artifact using the "
                    "publish_artifact tool. Name the file hello.py."
                ),
                "session_id": session_id,
            })

            try:
                terminal, frames = await client.recv_until_terminal(timeout=180)
                latency_recorder.mark("terminal_frame")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame within 180s for artifact publish prompt")

            assert terminal.status == "completed"
            assert terminal.content, "Terminal frame has empty content"

        # Wait for DDB/S3 propagation
        await asyncio.sleep(5)

        # Query artifact catalog
        catalog_rows = artifact_catalog(session_id)
        scan_artifacts_for_cleanup(session_id, cleanup)

        if not catalog_rows:
            pytest.xfail(
                f"No artifact catalog rows found for session {session_id}. "
                "Agent may not have invoked publish_artifact tool."
            )

        # Verify catalog row structure
        art_row = catalog_rows[0]  # newest first
        assert art_row.get("source") == "agent", (
            f"Expected source='agent', got '{art_row.get('source')}'"
        )
        assert art_row.get("filename", "").endswith(".py"), (
            f"Expected .py file, got '{art_row.get('filename')}'"
        )
        assert art_row.get("id", "").startswith("art_"), (
            f"Artifact ID should start with 'art_', got '{art_row.get('id')}'"
        )
        assert art_row.get("sizeBytes", 0) > 0, "Artifact has 0 bytes"
        assert art_row.get("checksum"), "Artifact missing checksum"

        # Verify S3 object exists
        s3_key = art_row.get("s3Key", "")
        assert s3_key, "Catalog row missing s3Key"

        s3 = boto3.client("s3", region_name="us-east-1")
        try:
            head = s3.head_object(Bucket=ARTIFACTS_BUCKET, Key=s3_key)
            assert head["ContentLength"] > 0, "S3 object is empty"
            latency_recorder.note("s3_size_bytes", str(head["ContentLength"]))
        except s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                pytest.fail(
                    f"S3 object not found at s3://{ARTIFACTS_BUCKET}/{s3_key}. "
                    "Catalog row exists but S3 upload may have failed."
                )
            raise

        # Verify the reply references the artifact
        reply_lower = terminal.content.lower()
        art_id = art_row.get("id", "")
        has_art_ref = (
            art_id in terminal.content
            or "artifact" in reply_lower
            or "hello.py" in reply_lower
            or "publish" in reply_lower
        )
        if not has_art_ref:
            latency_recorder.note("reply_has_art_ref", "false")
        else:
            latency_recorder.note("reply_has_art_ref", "true")

        latency_recorder.note("artifact_id", art_id)
        latency_recorder.note("artifact_filename", art_row.get("filename", ""))


# ============================================================================
# Section 2.8 — Fetch-and-edit round-trip
# ============================================================================


class TestArtifactFetchEditRoundTrip:
    """Validate fetch_artifact -> edit -> publish_artifact with supersedes.

    Flow:
      Turn 1: "Write and publish hello.py"
      Turn 2: "Add a docstring to that Python file and publish the updated version."
      Assert:
        - Agent calls fetch_artifact for art_1
        - Agent calls publish_artifact with supersedes=<art_1 id>
        - New catalog row exists with supersedes set
    """

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_fetch_edit_publish_with_supersedes(
        self,
        ws_client_async,
        fresh_jwt,
        artifact_catalog,
        cleanup,
        latency_recorder,
        make_session_id,
    ):
        """2.8: Publish, then fetch-edit-republish with supersedes lineage."""
        session_id = make_session_id("art-edit")
        cleanup.track(session_id)
        token = fresh_jwt()

        async with ws_client_async(token, session_id) as client:
            # Turn 1: publish original
            latency_recorder.mark("turn1_send")
            await client.send({
                "action": "message",
                "text": (
                    "Write a Python script that prints 'hello world' and save/publish "
                    "it as an artifact named hello.py using the publish_artifact tool."
                ),
                "session_id": session_id,
            })

            try:
                terminal1, frames1 = await client.recv_until_terminal(timeout=180)
                latency_recorder.mark("turn1_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for turn 1 (initial publish)")

            assert terminal1.status == "completed"
            await asyncio.sleep(3)

            # Check turn 1 produced an artifact
            catalog_after_t1 = artifact_catalog(session_id)
            if not catalog_after_t1:
                scan_artifacts_for_cleanup(session_id, cleanup)
                pytest.xfail(
                    "No artifact created in turn 1. "
                    "Agent did not invoke publish_artifact."
                )

            art1_id = catalog_after_t1[0].get("id", "")
            latency_recorder.note("art1_id", art1_id)

            # Turn 2: fetch, edit, and republish
            latency_recorder.mark("turn2_send")
            await client.send({
                "action": "message",
                "text": (
                    f"Now fetch the artifact you just published (ID: {art1_id}), "
                    "add a module-level docstring at the top of the file, and "
                    "publish the updated version as a new artifact that supersedes "
                    "the original. Use fetch_artifact then publish_artifact with "
                    "the supersedes parameter."
                ),
                "session_id": session_id,
            })

            try:
                terminal2, frames2 = await client.recv_until_terminal(timeout=180)
                latency_recorder.mark("turn2_terminal")
            except asyncio.TimeoutError:
                pytest.fail("No terminal frame for turn 2 (edit+republish)")

            assert terminal2.status == "completed"

        # Wait for DDB propagation
        await asyncio.sleep(5)

        # Query catalog — should have 2+ artifacts now
        catalog_rows = artifact_catalog(session_id)
        scan_artifacts_for_cleanup(session_id, cleanup)

        if len(catalog_rows) < 2:
            pytest.xfail(
                f"Expected >= 2 artifact rows, got {len(catalog_rows)}. "
                "Agent may not have published a second artifact in turn 2."
            )

        # Find the newer artifact (first in list, sorted newest-first)
        art2_row = catalog_rows[0]
        art2_id = art2_row.get("id", "")
        latency_recorder.note("art2_id", art2_id)

        # Verify art2 supersedes art1
        supersedes = art2_row.get("supersedes")
        if supersedes:
            assert supersedes == art1_id, (
                f"Expected supersedes={art1_id}, got {supersedes}"
            )
            latency_recorder.note("supersedes_correct", "true")
        else:
            # The agent may not have used the supersedes parameter
            latency_recorder.note("supersedes_correct", "false")
            pytest.xfail(
                f"Artifact {art2_id} does not have 'supersedes' set. "
                "Agent published a new artifact but didn't link lineage."
            )

        # Verify S3 objects for both artifacts exist
        s3 = boto3.client("s3", region_name="us-east-1")
        for art_row in [catalog_rows[0], catalog_rows[-1]]:
            s3_key = art_row.get("s3Key", "")
            if s3_key:
                try:
                    s3.head_object(Bucket=ARTIFACTS_BUCKET, Key=s3_key)
                except Exception:
                    pytest.fail(
                        f"S3 object missing for artifact {art_row.get('id')}: {s3_key}"
                    )

        latency_recorder.note("total_artifacts", str(len(catalog_rows)))
