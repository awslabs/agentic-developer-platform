"""The chat supervisor ScaledJob must scale on visible queue messages only (#8047).

KEDA's aws-sqs-queue scaler counts in-flight (not-visible) messages by default. The chat
delivery a running supervisor holds stays in flight for the 900 s visibility timeout, so with
the default every stuck turn started a new supervisor every ~25 s that received nothing and
exited. Both sources of the trigger pin ``scaleOnInFlight: "false"``: the checked-in manifest
and the trigger rendered by ``deploy-chat-scaledjob.sh`` (the one applied on dev).
"""

import re
from pathlib import Path

import yaml


K8S = Path(__file__).resolve().parents[2] / "agent/k8s"


def test_checked_in_chat_scaledjob_counts_visible_messages_only():
    documents = list(yaml.safe_load_all((K8S / "chat-scaledjob.yaml").read_text()))
    scaledjob = next(doc for doc in documents if doc and doc.get("kind") == "ScaledJob")
    assert scaledjob["metadata"]["name"] == "chat-agent-worker"
    (trigger,) = scaledjob["spec"]["triggers"]
    assert trigger["type"] == "aws-sqs-queue"
    assert trigger["metadata"]["queueLength"] == "1"
    assert trigger["metadata"]["scaleOnInFlight"] == "false"


def test_deploy_script_renders_the_same_trigger_metadata():
    script = (K8S / "deploy-chat-scaledjob.sh").read_text()
    trigger = re.search(r'"triggers": \[\{"type": "aws-sqs-queue".*?"metadata": \{(?P<metadata>[^}]*)\}', script, re.DOTALL)
    assert trigger, "deploy-chat-scaledjob.sh must render the aws-sqs-queue trigger"
    metadata = trigger.group("metadata")
    assert '"queueLength": "1"' in metadata
    assert '"scaleOnInFlight": "false"' in metadata
